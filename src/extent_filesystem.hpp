// In-process adapter to the pinned drive9 extent runtime (bridge/main.go).
// Paths, blocks, sessions and locks are the existing filesystem's own objects.
extern "C" char *tidbfs_extent_call(char *request);
extern "C" void tidbfs_extent_free(char *response);

nlohmann::json ExtentCall(nlohmann::json request) {
	auto raw = request.dump();
	std::unique_ptr<char, decltype(&tidbfs_extent_free)> response(tidbfs_extent_call(&raw[0]), tidbfs_extent_free);
	if (!response)
		throw IOException("Empty extent bridge response");
	auto result = nlohmann::json::parse(response.get());
	if (result.contains("error"))
		throw IOException("tidbfs extent: %s", JSONString(result, "error"));
	return result;
}

string ExtentPath(const string &input) {
	auto uri = NormalizeURI(input);
	if (uri.compare(0, 13, "tidbfs+db:///") == 0)
		return Path("tidbfs://" + uri.substr(12));
	return Path(uri);
}

struct ExtentClient {
	uint64_t session;
	explicit ExtentClient(const Config &config) {
		session = uint64_t(JSONNumber(
			ExtentCall({{"op", "connect"}, {"endpoint", config.endpoint}, {"token", config.token}}), "session"));
	}
	~ExtentClient() {
		try {
			Call({{"op", "disconnect"}});
		} catch (...) {
		}
	}
	nlohmann::json Call(nlohmann::json request) {
		request["session"] = session;
		return ExtentCall(std::move(request));
	}
};

struct ExtentHandle : FileHandle {
	std::shared_ptr<ExtentClient> client;
	uint64_t handle;
	idx_t position = 0, size;
	bool closed = false;
	std::mutex mu;
	ExtentHandle(FileSystem &fs, const string &path, FileOpenFlags flags, std::shared_ptr<ExtentClient> c,
				 const nlohmann::json &info)
		: FileHandle(fs, path, flags), client(std::move(c)), handle(uint64_t(JSONNumber(info, "handle"))),
		  size(idx_t(JSONNumber(info, "size"))) {
		if (flags.OpenForAppending())
			position = size;
	}
	~ExtentHandle() override {
		try {
			Close();
		} catch (...) {
		}
	}
	void Close() override {
		std::lock_guard<std::mutex> lock(mu);
		if (closed)
			return;
		closed = true;
		client->Call({{"op", "close"}, {"handle", handle}});
	}
	void Check(bool writing = false) {
		if (closed)
			throw IOException("Extent handle is closed");
		if (writing && !flags.OpenForWriting())
			throw IOException("Extent handle is read only");
	}
	idx_t ReadAt(void *buffer, idx_t count, idx_t offset) {
		Check();
		idx_t done = 0;
		while (done < count) {
			auto n = MinValue<idx_t>(1 << 20, count - done);
			auto result = client->Call({{"op", "read"}, {"handle", handle}, {"offset", offset + done}, {"length", n}});
			auto data = Blob::FromBase64(string_t(JSONString(result, "data")));
			if (data.size() > n)
				throw IOException("Oversized extent read");
			memcpy(static_cast<char *>(buffer) + done, data.data(), data.size());
			done += data.size();
			if (data.size() < n)
				break;
		}
		return done;
	}
	void WriteAt(void *buffer, idx_t count, idx_t offset) {
		Check(true);
		if (offset > idx_t(std::numeric_limits<int64_t>::max()) ||
			count > idx_t(std::numeric_limits<int64_t>::max()) - offset)
			throw IOException("Extent write offset overflow");
		for (idx_t done = 0; done < count;) {
			auto n = MinValue<idx_t>(1 << 20, count - done);
			client->Call({{"op", "write"},
						  {"handle", handle},
						  {"offset", offset + done},
						  {"data", Blob::ToBase64(string_t(static_cast<const char *>(buffer) + done, n))}});
			done += n;
		}
		size = MaxValue<idx_t>(size, offset + count);
	}
};

class TiDBDatabaseFileSystem : public FileSystem {
	std::mutex mu;
	std::map<std::pair<string, string>, std::shared_ptr<ExtentClient>> clients;
	std::shared_ptr<ExtentClient> Client(const string &path, optional_ptr<FileOpener> opener) {
		auto config = ReadConfig(opener, path);
		std::lock_guard<std::mutex> lock(mu);
		auto &c = clients[{config.endpoint, config.token}];
		if (!c)
			c = std::make_shared<ExtentClient>(config);
		return c;
	}
	nlohmann::json Stat(const string &path, optional_ptr<FileOpener> opener) {
		return Client(path, opener)->Call({{"op", "stat"}, {"path", ExtentPath(path)}});
	}

  public:
	string GetName() const override { return "tidbfs_extent"; }
	bool CanHandleFile(const string &path) override { return NormalizeURI(path).compare(0, 13, "tidbfs+db:///") == 0; }
	bool CanSeek() override { return true; }
	bool OnDiskFile(FileHandle &) override { return false; }
	bool IsManuallySet() override { return true; }
	unique_ptr<FileHandle> OpenFile(const string &path, FileOpenFlags flags, optional_ptr<FileOpener> opener) override {
		auto c = Client(path, opener);
		auto p = ExtentPath(path);
		auto info = c->Call({{"op", "stat"}, {"path", p}});
		bool exists = info.at("exists").get<bool>();
		if ((!exists && flags.ReturnNullIfNotExists()) || (exists && flags.ReturnNullIfExists()))
			return nullptr;
		string lock;
		if (flags.Lock() == FileLockType::READ_LOCK)
			lock = "read";
		if (flags.Lock() == FileLockType::WRITE_LOCK)
			lock = "write";
		info = c->Call({{"op", "open"},
						{"path", p},
						{"write", flags.OpenForWriting()},
						{"create", flags.CreateFileIfNotExists() || flags.OverwriteExistingFile()},
						{"exclusive", flags.ExclusiveCreate()},
						{"truncate", flags.OverwriteExistingFile()},
						{"lock", lock}});
		return make_uniq<ExtentHandle>(*this, path, flags, std::move(c), info);
	}
	void Read(FileHandle &base, void *buffer, int64_t count, idx_t offset) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0 || h.ReadAt(buffer, idx_t(count), offset) != idx_t(count))
			throw IOException("Short extent read");
	}
	int64_t Read(FileHandle &base, void *buffer, int64_t count) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative read size");
		auto n = h.ReadAt(buffer, idx_t(count), h.position);
		h.position += n;
		return int64_t(n);
	}
	void Write(FileHandle &base, void *buffer, int64_t count, idx_t offset) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative write size");
		h.WriteAt(buffer, idx_t(count), offset);
	}
	int64_t Write(FileHandle &base, void *buffer, int64_t count) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0)
			throw IOException("Negative write size");
		auto offset = h.flags.OpenForAppending() ? h.size : h.position;
		h.WriteAt(buffer, idx_t(count), offset);
		h.position = offset + idx_t(count);
		return count;
	}
	void FileSync(FileHandle &base) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		h.client->Call({{"op", "sync"}, {"handle", h.handle}});
	}
	void Truncate(FileHandle &base, int64_t size) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check(true);
		if (size < 0)
			throw IOException("Negative truncate size");
		h.client->Call({{"op", "truncate"}, {"handle", h.handle}, {"offset", size}});
		h.size = idx_t(size);
	}
	int64_t GetFileSize(FileHandle &base) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		return int64_t(h.size);
	}
	timestamp_t GetLastModifiedTime(FileHandle &) override { return timestamp_t(0); }
	FileType GetFileType(FileHandle &) override { return FileType::FILE_TYPE_REGULAR; }
	void Seek(FileHandle &base, idx_t offset) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		h.position = offset;
	}
	idx_t SeekPosition(FileHandle &base) override {
		auto &h = base.Cast<ExtentHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.Check();
		return h.position;
	}
	bool FileExists(const string &path, optional_ptr<FileOpener> opener) override {
		auto i = Stat(path, opener);
		return i.at("exists").get<bool>() && !i.at("directory").get<bool>();
	}
	bool DirectoryExists(const string &path, optional_ptr<FileOpener> opener) override {
		auto i = Stat(path, opener);
		return i.at("exists").get<bool>() && i.at("directory").get<bool>();
	}
	void CreateDirectory(const string &path, optional_ptr<FileOpener> opener) override {
		if (!DirectoryExists(path, opener))
			Client(path, opener)->Call({{"op", "mkdir"}, {"path", ExtentPath(path)}});
	}
	bool TryRemoveFile(const string &path, optional_ptr<FileOpener> opener) override {
		if (!FileExists(path, opener))
			return false;
		Client(path, opener)->Call({{"op", "remove"}, {"path", ExtentPath(path)}});
		return true;
	}
	void RemoveFile(const string &path, optional_ptr<FileOpener> opener) override {
		if (!TryRemoveFile(path, opener))
			throw IOException("Extent file does not exist");
	}
	bool ListFiles(const string &path, const std::function<void(const string &, bool)> &callback,
				   FileOpener *opener) override {
		if (!DirectoryExists(path, opener))
			return false;
		auto result = Client(path, opener)->Call({{"op", "list"}, {"path", ExtentPath(path)}});
		for (auto &entry : result.at("entries"))
			callback(JSONString(entry, "name"), entry.at("directory").get<bool>());
		return true;
	}
	void RemoveDirectory(const string &path, optional_ptr<FileOpener> opener) override {
		vector<std::pair<string, bool>> entries;
		ListFiles(path, [&](const string &name, bool dir) { entries.emplace_back(name, dir); }, opener.get());
		for (auto &entry : entries) {
			auto p = path + "/" + entry.first;
			if (entry.second)
				RemoveDirectory(p, opener);
			else
				RemoveFile(p, opener);
		}
		Client(path, opener)->Call({{"op", "remove"}, {"path", ExtentPath(path)}});
	}
	void MoveFile(const string &source, const string &target, optional_ptr<FileOpener> opener) override {
		auto a = ReadConfig(opener, source), b = ReadConfig(opener, target);
		if (a.endpoint != b.endpoint || a.token != b.token)
			throw IOException("Cannot move across filesystems");
		Client(source, opener)->Call({{"op", "rename"}, {"path", ExtentPath(source)}, {"target", ExtentPath(target)}});
	}
	vector<OpenFileInfo> Glob(const string &path, FileOpener *) override {
		if (HasGlob(path))
			throw NotImplementedException("Use tidbfs:/// for analytical globs");
		return {OpenFileInfo(path)};
	}
};
