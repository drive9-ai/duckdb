// Included inside the tidbfs implementation namespace, after HTTP helpers.
// This is a separate namespace from analytical files: ordinary PUT/multipart
// writers cannot bypass database leases or replace a database generation.
struct DatabasePath {
	string root, name;
	explicit DatabasePath(const string &uri) {
		const string prefix = "tidbfs+db://";
		if (uri.compare(0, prefix.size(), prefix) != 0)
			throw InvalidInputException("Invalid database URI");
		auto path = Path("tidbfs://" + uri.substr(prefix.size()));
		auto end = path.find(".duckdb");
		if (end == string::npos)
			throw InvalidInputException("Database URI must end in .duckdb (or its WAL/temp suffix)");
		end += 7;
		root = path.substr(0, end);
		name = path.substr(end);
		while (!name.empty() && name.back() == '/')
			name.pop_back();
		if (!name.empty() && name[0] != '.')
			throw InvalidInputException("Invalid database sidecar URI");
	}
};

nlohmann::json DatabaseRPC(const Config &config, nlohmann::json request) {
	HTTP http;
	auto response = PostJSON(http, config, "/v1/duckdb", request);
	if (response.status == 501)
		throw IOException("Enable experimental DuckDB storage on a local-provider fs server");
	if (response.status == 409)
		throw IOException("Remote DuckDB database is locked or the file already exists");
	if (response.status == 410)
		throw IOException("Remote DuckDB lease expired; reopen the database");
	auto result = JSON(response);
	if (JSONNumber(result, "version") != 1)
		throw IOException("Unsupported remote database protocol version");
	return result;
}

struct DatabaseLease {
	Config config;
	string root, token;
	bool read_only;
	std::atomic<bool> main_open{false};
	std::mutex mu;
	std::condition_variable cv;
	bool stopped = false, failed = false;
	std::thread heartbeat;
	DatabaseLease(Config cfg, string path, bool ro, bool create)
	    : config(std::move(cfg)), root(std::move(path)), read_only(ro) {
		auto result =
		    DatabaseRPC(config, {{"op", "acquire"}, {"database", root}, {"read_only", ro}, {"create", create}});
		token = JSONString(result, "token");
		if (JSONNumber(result, "lease_ms") != 30000)
			throw IOException("Unsupported remote database lease duration");
		heartbeat = std::thread([this] {
			std::unique_lock<std::mutex> lock(mu);
			while (!cv.wait_for(lock, std::chrono::seconds(5), [this] { return stopped; })) {
				lock.unlock();
				try {
					Call({{"op", "renew"}});
				} catch (...) {
					lock.lock();
					failed = true;
					break;
				}
				lock.lock();
			}
		});
	}
	~DatabaseLease() {
		{
			std::lock_guard<std::mutex> lock(mu);
			stopped = true;
			cv.notify_all();
		}
		if (heartbeat.joinable())
			heartbeat.join();
		// No dirty bytes are buffered by this filesystem. A lost release response
		// only delays the next owner until server-clock expiry; never retry it.
		try {
			DatabaseRPC(config, {{"op", "release"}, {"database", root}, {"token", token}});
		} catch (...) {
		}
	}
	nlohmann::json Call(nlohmann::json request) {
		std::lock_guard<std::mutex> lock(mu);
		if (failed)
			throw IOException("Remote DuckDB session failed; reopen the database (do not retry a commit)");
		request["database"] = root;
		request["token"] = token;
		try {
			return DatabaseRPC(config, std::move(request));
		} catch (...) {
			failed = true;
			throw;
		}
	}
};

struct DatabaseHandle : FileHandle {
	std::shared_ptr<DatabaseLease> lease;
	string id;
	idx_t position = 0, size = 0;
	bool closed = false;
	bool owns_main;
	std::mutex mu;
	DatabaseHandle(FileSystem &fs, const string &path, FileOpenFlags flags, std::shared_ptr<DatabaseLease> owner,
	               const nlohmann::json &info, bool main)
	    : FileHandle(fs, path, flags), lease(std::move(owner)), id(JSONString(info, "file_id")),
	      size(idx_t(JSONNumber(info, "size"))), owns_main(main) {
		if (flags.OpenForAppending())
			position = size;
	}
	~DatabaseHandle() override { Close(); }
	void Close() override {
		std::lock_guard<std::mutex> lock(mu);
		closed = true;
		if (lease && owns_main)
			lease->main_open.store(false);
		lease.reset();
	}
	void Check(bool write = false) {
		if (closed || !lease)
			throw IOException("Remote DuckDB file is closed");
		if (write && (!flags.OpenForWriting() || lease->read_only))
			throw IOException("Remote DuckDB file is read only");
	}
	idx_t ReadAt(void *buffer, idx_t count, idx_t offset) {
		Check();
		idx_t done = 0;
		while (done < count) {
			auto n = MinValue<idx_t>(1 << 20, count - done);
			auto result = lease->Call({{"op", "read"}, {"file_id", id}, {"offset", offset + done}, {"length", n}});
			auto data = result.contains("data") ? Blob::FromBase64(string_t(JSONString(result, "data"))) : string();
			if (data.size() > n)
				throw IOException("Oversized remote database read");
			memcpy(static_cast<char *>(buffer) + done, data.data(), data.size());
			done += data.size();
			size = idx_t(JSONNumber(result, "size"));
			if (data.size() < n)
				break;
		}
		return done;
	}
	void WriteAt(void *buffer, idx_t count, idx_t offset) {
		Check(true);
		if (offset > (1ULL << 30) || count > (1ULL << 30) - offset)
			throw IOException("Experimental remote database size limit exceeded");
		for (idx_t done = 0; done < count;) {
			auto n = MinValue<idx_t>(1 << 20, count - done);
			auto result =
			    lease->Call({{"op", "write"},
			                 {"file_id", id},
			                 {"offset", offset + done},
			                 {"data", Blob::ToBase64(string_t(static_cast<const char *>(buffer) + done, n))}});
			size = idx_t(JSONNumber(result, "size"));
			done += n;
		}
	}
};

class TiDBDatabaseFileSystem : public FileSystem {
	std::mutex mu;
	std::map<std::tuple<string, string, string>, std::weak_ptr<DatabaseLease>> leases;
	// DuckDB destroys attached databases before its temporary-directory manager.
	// Keep ownership until spill cleanup has finished, even after main-file close.
	std::map<std::tuple<string, string, string>, std::shared_ptr<DatabaseLease>> directories;
	std::shared_ptr<DatabaseLease> Find(const Config &config, const DatabasePath &path) {
		auto key = std::make_tuple(config.endpoint, config.token, path.root);
		auto found = leases.find(key);
		return found == leases.end() ? nullptr : found->second.lock();
	}
	std::shared_ptr<DatabaseLease> Owner(const string &path, optional_ptr<FileOpener> opener) {
		auto config = ReadConfig(opener, path);
		DatabasePath parsed(path);
		std::lock_guard<std::mutex> lock(mu);
		auto owner = Find(config, parsed);
		if (!owner)
			throw IOException("Open the remote DuckDB database before accessing its sidecars");
		return owner;
	}
	nlohmann::json Stat(const string &path, optional_ptr<FileOpener> opener) {
		auto config = ReadConfig(opener, path);
		DatabasePath parsed(path);
		std::shared_ptr<DatabaseLease> owner;
		{
			std::lock_guard<std::mutex> lock(mu);
			owner = Find(config, parsed);
		}
		if (owner)
			return owner->Call({{"op", "stat"}, {"name", parsed.name}});
		return DatabaseRPC(config, {{"op", "probe"}, {"database", parsed.root}, {"name", parsed.name}});
	}

  public:
	string GetName() const override { return "tidbfs_database"; }
	bool CanHandleFile(const string &path) override { return path.compare(0, 13, "tidbfs+db:///") == 0; }
	bool CanSeek() override { return true; }
	bool OnDiskFile(FileHandle &) override { return false; }
	bool IsManuallySet() override { return true; }
	unique_ptr<FileHandle> OpenFile(const string &path, FileOpenFlags flags, optional_ptr<FileOpener> opener) override {
		DatabasePath parsed(path);
		auto config = ReadConfig(opener, path);
		if (!flags.OpenForReading() && !flags.OpenForWriting())
			throw InvalidInputException("Database file requires read or write access");
		if (flags.CreatePrivateFile())
			throw NotImplementedException("Remote database permissions are controlled by filesystem credentials");
		auto info = Stat(path, opener);
		bool exists = info.at("exists").get<bool>();
		if ((!exists && flags.ReturnNullIfNotExists()) || (exists && flags.ReturnNullIfExists()))
			return nullptr;
		std::shared_ptr<DatabaseLease> owner;
		{
			std::lock_guard<std::mutex> lock(mu);
			owner = Find(config, parsed);
			if (!owner) {
				if (!parsed.name.empty())
					throw IOException("Open the database before opening a WAL or temporary file");
				owner = std::make_shared<DatabaseLease>(config, parsed.root, !flags.OpenForWriting(),
				                                        flags.CreateFileIfNotExists() || flags.OverwriteExistingFile());
				leases[std::make_tuple(config.endpoint, config.token, parsed.root)] = owner;
			}
		}
		bool main = parsed.name.empty() && flags.Lock() != FileLockType::NO_LOCK;
		if (main && owner->main_open.exchange(true))
			throw IOException("Remote DuckDB database is already attached in this instance");
		try {
			info = owner->Call({{"op", "open"},
			                    {"name", parsed.name},
			                    {"create", flags.CreateFileIfNotExists() || flags.OverwriteExistingFile()},
			                    {"truncate", flags.OverwriteExistingFile()},
			                    {"exclusive", flags.ExclusiveCreate()}});
			return make_uniq<DatabaseHandle>(*this, path, flags, owner, info, main);
		} catch (...) {
			if (main)
				owner->main_open.store(false);
			throw;
		}
	}
	void Read(FileHandle &base, void *buffer, int64_t count, idx_t offset) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0 || h.ReadAt(buffer, idx_t(count), offset) != idx_t(count))
			throw IOException("Short remote database read");
	}
	int64_t Read(FileHandle &base, void *buffer, int64_t count) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative read size");
		auto n = h.ReadAt(buffer, idx_t(count), h.position);
		h.position += n;
		return int64_t(n);
	}
	void Write(FileHandle &base, void *buffer, int64_t count, idx_t offset) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative write size");
		h.WriteAt(buffer, idx_t(count), offset);
	}
	int64_t Write(FileHandle &base, void *buffer, int64_t count) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative write size");
		auto offset = h.flags.OpenForAppending() ? h.size : h.position;
		h.WriteAt(buffer, idx_t(count), offset);
		h.position = offset + idx_t(count);
		return count;
	}
	void FileSync(FileHandle &base) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		h.lease->Call({{"op", "sync"}});
	}
	void Truncate(FileHandle &base, int64_t size) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check(true);
		if (size < 0)
			throw IOException("Negative truncate size");
		auto info = h.lease->Call({{"op", "truncate"}, {"file_id", h.id}, {"offset", size}});
		h.size = idx_t(JSONNumber(info, "size"));
	}
	int64_t GetFileSize(FileHandle &base) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		return int64_t(h.size);
	}
	timestamp_t GetLastModifiedTime(FileHandle &) override { return timestamp_t(0); }
	FileType GetFileType(FileHandle &) override { return FileType::FILE_TYPE_REGULAR; }
	void Seek(FileHandle &base, idx_t offset) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		h.position = offset;
	}
	idx_t SeekPosition(FileHandle &base) override {
		auto &h = base.Cast<DatabaseHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		return h.position;
	}
	bool FileExists(const string &path, optional_ptr<FileOpener> opener) override {
		auto info = Stat(path, opener);
		return info.at("exists").get<bool>() && !info.at("directory").get<bool>();
	}
	bool DirectoryExists(const string &path, optional_ptr<FileOpener> opener) override {
		auto info = Stat(path, opener);
		return info.at("exists").get<bool>() && info.at("directory").get<bool>();
	}
	void CreateDirectory(const string &path, optional_ptr<FileOpener> opener) override {
		auto owner = Owner(path, opener);
		owner->Call({{"op", "open"}, {"name", DatabasePath(path).name}, {"create", true}, {"directory", true}});
		std::lock_guard<std::mutex> lock(mu);
		directories[std::make_tuple(owner->config.endpoint, owner->config.token,
		                            owner->root + DatabasePath(path).name)] = owner;
	}
	bool TryRemoveFile(const string &path, optional_ptr<FileOpener> opener) override {
		if (!FileExists(path, opener))
			return false;
		Owner(path, opener)->Call({{"op", "remove"}, {"name", DatabasePath(path).name}});
		return true;
	}
	void RemoveFile(const string &path, optional_ptr<FileOpener> opener) override {
		if (!TryRemoveFile(path, opener))
			throw IOException("Remote database file does not exist");
	}
	bool ListFiles(const string &path, const std::function<void(const string &, bool)> &callback,
	               FileOpener *opener) override {
		if (!DirectoryExists(path, opener))
			return false;
		auto info = Owner(path, opener)->Call({{"op", "list"}, {"name", DatabasePath(path).name}});
		if (info.contains("entries"))
			for (auto &entry : info["entries"])
				callback(JSONString(entry, "name"), entry.at("directory").get<bool>());
		return true;
	}
	void RemoveDirectory(const string &path, optional_ptr<FileOpener> opener) override {
		vector<std::pair<string, bool>> entries;
		ListFiles(path, [&](const string &name, bool dir) { entries.emplace_back(name, dir); }, opener.get());
		for (auto &entry : entries) {
			auto child = path + "/" + entry.first;
			if (entry.second)
				RemoveDirectory(child, opener);
			else
				RemoveFile(child, opener);
		}
		auto owner = Owner(path, opener);
		owner->Call({{"op", "remove"}, {"name", DatabasePath(path).name}});
		std::lock_guard<std::mutex> lock(mu);
		directories.erase(
		    std::make_tuple(owner->config.endpoint, owner->config.token, owner->root + DatabasePath(path).name));
	}
	void MoveFile(const string &source, const string &target, optional_ptr<FileOpener> opener) override {
		DatabasePath from(source), to(target);
		auto a = ReadConfig(opener, source), b = ReadConfig(opener, target);
		if (from.root != to.root || a.endpoint != b.endpoint || a.token != b.token)
			throw IOException("Cannot move across database namespaces");
		Owner(source, opener)->Call({{"op", "rename"}, {"name", from.name}, {"target", to.name}});
	}
	vector<OpenFileInfo> Glob(const string &path, FileOpener *) override {
		if (HasGlob(path))
			throw NotImplementedException("Use tidbfs:/// for analytical file globs");
		return {OpenFileInfo(path)};
	}
};
