#define DUCKDB_EXTENSION_MAIN

#include "duckdb.hpp"
#include "duckdb/catalog/catalog_transaction.hpp"
#include "duckdb/common/file_opener.hpp"
#include "duckdb/common/file_system.hpp"
#include "duckdb/common/types/timestamp.hpp"
#include "duckdb/main/config.hpp"
#include "duckdb/main/client_context_state.hpp"
#include "duckdb/main/extension/extension_loader.hpp"
#include "duckdb/main/secret/secret_manager.hpp"
#include "json.hpp"

#include <curl/curl.h>
#include <fnmatch.h>

#include <algorithm>
#include <charconv>
#include <cstring>
#include <limits>
#include <map>
#include <mutex>
#include <set>
#include <tuple>

namespace duckdb {
namespace {

constexpr idx_t MAX_READ = 8 * 1024 * 1024;
constexpr idx_t MAX_CACHE = 1024 * 1024;
constexpr idx_t MAX_LIST = 16 * 1024 * 1024;

string Path(const string &uri) {
	if (uri.compare(0, 10, "tidbfs:///") != 0) {
		throw InvalidInputException("Use an absolute tidbfs:///path URI");
	}
	auto path = uri.substr(9);
	if (path.find('\\') != string::npos || path.find('\0') != string::npos ||
	    path.find("//") != string::npos) {
		throw InvalidInputException("Invalid tidbfs path");
	}
	for (auto &part : StringUtil::Split(path, '/')) {
		if (part == "." || part == "..") {
			throw InvalidInputException("Invalid tidbfs path component");
		}
	}
	return path;
}

string EncodePath(const string &path) {
	static const char hex[] = "0123456789ABCDEF";
	string result;
	for (unsigned char c : path) {
		if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
		    c == '/' || c == '-' || c == '_' || c == '.' || c == '~') {
			result += char(c);
		} else {
			result += '%';
			result += hex[c >> 4];
			result += hex[c & 15];
		}
	}
	return result;
}

int64_t Number(const string &text) {
	int64_t value = 0;
	auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
	if (text.empty() || parsed.ec != std::errc() || parsed.ptr != text.data() + text.size() || value < 0) {
		throw IOException("Invalid numeric tidbfs response header");
	}
	return value;
}

struct URL {
	CURLU *value = curl_url();
	explicit URL(const string &text) {
		if (!value || curl_url_set(value, CURLUPART_URL, text.c_str(), 0) != CURLUE_OK) {
			if (value) {
				curl_url_cleanup(value);
			}
			throw InvalidInputException("Invalid tidbfs HTTP URL");
		}
	}
	~URL() { curl_url_cleanup(value); }
	string Get(CURLUPart part) const {
		char *out = nullptr;
		if (curl_url_get(value, part, &out, 0) != CURLUE_OK) {
			return "";
		}
		string result(out);
		curl_free(out);
		return result;
	}
	bool Loopback() const {
		auto host = Get(CURLUPART_HOST);
		return host == "localhost" || host == "127.0.0.1" || host == "[::1]";
	}
};

void CheckEndpoint(const string &endpoint) {
	URL url(endpoint);
	if ((!url.Get(CURLUPART_USER).empty()) || !url.Get(CURLUPART_PASSWORD).empty() ||
	    !url.Get(CURLUPART_QUERY).empty() || !url.Get(CURLUPART_FRAGMENT).empty() ||
	    (url.Get(CURLUPART_SCHEME) != "https" && !(url.Get(CURLUPART_SCHEME) == "http" && url.Loopback()))) {
		throw InvalidInputException("tidbfs endpoint requires HTTPS (HTTP is allowed only on loopback)");
	}
}

string ObjectURL(const string &endpoint, const string &location) {
	URL base(endpoint);
	URL target(endpoint);
	if (location.empty() || curl_url_set(target.value, CURLUPART_URL, location.c_str(), 0) != CURLUE_OK) {
		throw IOException("Invalid tidbfs object redirect");
	}
	bool local = base.Get(CURLUPART_SCHEME) == "http" && base.Loopback() &&
	             target.Get(CURLUPART_SCHEME) == "http" && target.Get(CURLUPART_HOST) == base.Get(CURLUPART_HOST) &&
	             target.Get(CURLUPART_PORT) == base.Get(CURLUPART_PORT);
	if ((target.Get(CURLUPART_SCHEME) != "https" && !local) || !target.Get(CURLUPART_USER).empty() ||
	    !target.Get(CURLUPART_PASSWORD).empty() || !target.Get(CURLUPART_FRAGMENT).empty()) {
		throw IOException("Unsafe tidbfs object redirect");
	}
	return target.Get(CURLUPART_URL);
}

struct Config {
	string endpoint;
	string token;
	idx_t read_ahead = 65536;
};

// DuckDB can open one handle per row group. Keep version and object resolution
// query-local, so those handles use the same generation without N HEAD/redirects.
// Byte caches remain handle-local: their lifetime and memory scale with active
// readers, not with every file ever mentioned by a connection.
struct FileVersion {
	std::mutex mu;
	bool initialized = false;
	idx_t size = 0;
	int64_t mtime = 0;
	string etag, object_url, object_etag;
};

struct QueryFiles : ClientContextState {
	std::mutex mu;
	std::map<std::tuple<string, string, string>, shared_ptr<FileVersion>> files;
	shared_ptr<FileVersion> Get(const Config &config, const string &path) {
		std::lock_guard<std::mutex> lock(mu);
		auto &version = files[std::make_tuple(config.endpoint, config.token, path)];
		if (!version) {
			version = make_shared_ptr<FileVersion>();
		}
		return version;
	}
	void QueryEnd() override {
		std::lock_guard<std::mutex> lock(mu);
		files.clear();
	}
};

Config ReadConfig(optional_ptr<FileOpener> opener, const string &path) {
	auto manager = FileOpener::TryGetSecretManager(opener);
	auto transaction = FileOpener::TryGetCatalogTransaction(opener);
	if (!manager || !transaction) {
		throw IOException("tidbfs requires a DuckDB secret and a file opener");
	}
	auto match = manager->LookupSecret(*transaction, path, "tidbfs");
	if (!match.HasMatch()) {
		throw IOException("Create a tidbfs secret with ENDPOINT and TOKEN before opening files");
	}
	// The tidbfs secret factory and deserializer both produce KeyValueSecret.
	auto &secret = static_cast<const KeyValueSecret &>(match.GetSecret());
	Value endpoint, token, read_ahead;
	if (!secret.TryGetValue("endpoint", endpoint) || !secret.TryGetValue("token", token)) {
		throw IOException("tidbfs secret is missing ENDPOINT or TOKEN");
	}
	Config config;
	config.endpoint = endpoint.GetValue<string>();
	config.token = token.GetValue<string>();
	CheckEndpoint(config.endpoint);
	if (config.token.empty() || config.token.find_first_of("\r\n") != string::npos) {
		throw InvalidInputException("Invalid tidbfs token");
	}
	while (!config.endpoint.empty() && config.endpoint.back() == '/') {
		config.endpoint.pop_back();
	}
	if (FileOpener::TryGetCurrentSetting(opener, "tidbfs_read_ahead", read_ahead)) {
		auto size = read_ahead.GetValue<int64_t>();
		if (size < 0 || size > int64_t(MAX_CACHE)) {
			throw InvalidInputException("tidbfs_read_ahead must be between 0 and 1048576 bytes");
		}
		config.read_ahead = idx_t(size);
	}
	return config;
}

struct Response {
	long status = 0;
	string body;
	std::map<string, string> headers;
	idx_t limit;
	string Header(const string &name) const {
		auto it = headers.find(name);
		return it == headers.end() ? "" : it->second;
	}
};

class HTTP {
public:
	HTTP() : curl(curl_easy_init()) {
		if (!curl) {
			throw IOException("Cannot initialize tidbfs HTTP client");
		}
	}
	~HTTP() { curl_easy_cleanup(curl); }
	HTTP(const HTTP &) = delete;
	HTTP &operator=(const HTTP &) = delete;

	Response Request(const string &url, const string &token, bool head, const vector<string> &headers, idx_t limit) {
		Response response;
		response.limit = MaxValue<idx_t>(limit, 65536); // bounded redirect/error bodies
		curl_easy_reset(curl); // preserves this handle's connection pool
		curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
		curl_easy_setopt(curl, CURLOPT_NOBODY, head ? 1L : 0L);
		curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
		curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTP | CURLPROTO_HTTPS);
		curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
		curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 10L);
		curl_easy_setopt(curl, CURLOPT_TIMEOUT, 120L);
		curl_easy_setopt(curl, CURLOPT_HTTP_CONTENT_DECODING, 0L);
		curl_easy_setopt(curl, CURLOPT_USERAGENT, "duckdb-tidbfs/0.1.0");
		curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, Body);
		curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
		curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, Header);
		curl_easy_setopt(curl, CURLOPT_HEADERDATA, &response);
		curl_slist *list = nullptr;
		list = curl_slist_append(list, "Accept-Encoding: identity");
		if (!token.empty()) {
			list = curl_slist_append(list, ("Authorization: Bearer " + token).c_str());
		}
		for (auto &h : headers) {
			list = curl_slist_append(list, h.c_str());
		}
		curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
		auto code = curl_easy_perform(curl);
		curl_slist_free_all(list);
		if (code != CURLE_OK) {
			// Never include URLs, server bodies or curl's error buffer: signed URLs contain credentials.
			throw IOException("tidbfs HTTP transport failed (code %d)", int(code));
		}
		curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);
		return response;
	}

private:
	CURL *curl;
	static size_t Body(char *data, size_t size, size_t count, void *user) noexcept {
		auto &r = *static_cast<Response *>(user);
		auto length = size * count;
		if (length > r.limit - r.body.size()) {
			return 0;
		}
		try {
			r.body.append(data, length);
			return length;
		} catch (...) {
			return 0;
		}
	}
	static size_t Header(char *data, size_t size, size_t count, void *user) noexcept {
		auto &r = *static_cast<Response *>(user);
		auto length = size * count;
		try {
			string line(data, length);
			if (line.compare(0, 5, "HTTP/") == 0) {
				r.headers.clear(); // e.g. CONNECT or interim 100 response
			}
			auto colon = line.find(':');
			if (colon != string::npos) {
				auto value = line.substr(colon + 1);
				StringUtil::Trim(value);
				r.headers[StringUtil::Lower(line.substr(0, colon))] = value;
			}
			return length;
		} catch (...) {
			return 0;
		}
	}
};

void CheckStatus(const Response &r) {
	if (r.status == 412) {
		throw IOException("tidbfs file changed since it was opened; restart the query");
	}
	if (r.status < 200 || r.status >= 300) {
		throw IOException("tidbfs HTTP %d", int(r.status));
	}
}

struct TiDBHandle : FileHandle {
	TiDBHandle(FileSystem &fs, const string &path, FileOpenFlags flags, Config config, shared_ptr<FileVersion> version)
	    : FileHandle(fs, path, flags), config(std::move(config)), version(std::move(version)) {
	}
	Config config;
	shared_ptr<FileVersion> version;
	HTTP http;
	std::mutex mu;
	idx_t size = 0, position = 0, cache_start = 0;
	int64_t mtime = 0;
	string etag, cache;
	bool closed = false;
	string Endpoint() const { return config.endpoint + "/v1/fs" + EncodePath(Path(path)); }
	void Close() override {
		std::lock_guard<std::mutex> lock(mu);
		closed = true;
		cache.clear();
	}
	Response Resolve(idx_t offset, idx_t count) {
		auto range = "Range: bytes=" + std::to_string(offset) + "-" + std::to_string(offset + count - 1);
		auto r = http.Request(Endpoint(), config.token, false, {range, "If-Match: " + etag}, count);
		if (r.Header("etag") != etag && r.status < 400) {
			throw IOException("tidbfs file version changed or server lacks conditional reads");
		}
		if (r.status == 302 || r.status == 307) {
			{
				std::lock_guard<std::mutex> lock(version->mu);
				version->object_url = ObjectURL(Endpoint(), r.Header("location"));
			}
			return ReadObject(offset, count);
		}
		return r;
	}
	Response ReadObject(idx_t offset, idx_t count) {
		vector<string> headers {"Range: bytes=" + std::to_string(offset) + "-" + std::to_string(offset + count - 1)};
		string object_url;
		{
			std::lock_guard<std::mutex> lock(version->mu);
			object_url = version->object_url;
			if (!version->object_etag.empty()) {
				headers.push_back("If-Match: " + version->object_etag);
			}
		}
		if (object_url.empty()) {
			return Resolve(offset, count);
		}
		// The filesystem bearer token and filesystem ETag MUST NOT cross this boundary.
		auto r = http.Request(object_url, "", false, headers, count);
		if (r.status >= 200 && r.status < 300) {
			auto tag = r.Header("etag");
			std::lock_guard<std::mutex> lock(version->mu);
			if (tag.empty() || tag.compare(0, 2, "W/") == 0 || (!version->object_etag.empty() && tag != version->object_etag)) {
				throw IOException("tidbfs backing object lacks a stable version");
			}
			version->object_etag = tag;
		}
		return r;
	}
	string Fetch(idx_t offset, idx_t count) {
		Response r;
		bool resolved;
		{
			std::lock_guard<std::mutex> lock(version->mu);
			resolved = !version->object_url.empty();
		}
		if (!resolved) {
			r = Resolve(offset, count);
		} else {
			r = ReadObject(offset, count);
			if (r.status == 401 || r.status == 403) {
				// One bounded refresh, with the original filesystem If-Match.
				{
					std::lock_guard<std::mutex> lock(version->mu);
					version->object_url.clear();
				}
				r = Resolve(offset, count);
			}
		}
		CheckStatus(r);
		auto expected = "bytes " + std::to_string(offset) + "-" + std::to_string(offset + count - 1) + "/" + std::to_string(size);
		if (r.status != 206 || r.Header("content-range") != expected || r.body.size() != count ||
		    (!r.Header("content-encoding").empty() && r.Header("content-encoding") != "identity")) {
			throw IOException("tidbfs expected an exact, uncompressed byte-range response");
		}
		return std::move(r.body);
	}
	void ReadAt(void *buffer, idx_t count, idx_t offset) {
		if (closed || offset > size || count > size - offset) {
			throw IOException("tidbfs read outside an open file");
		}
		auto out = static_cast<char *>(buffer);
		while (count) {
			if (offset >= cache_start && offset - cache_start < cache.size()) {
				auto available = MinValue<idx_t>(count, cache.size() - (offset - cache_start));
				memcpy(out, cache.data() + offset - cache_start, available);
				offset += available;
				out += available;
				count -= available;
				continue;
			}
			auto amount = MinValue<idx_t>(count, MAX_READ);
			auto begin = offset;
			auto end = offset + amount;
			if (config.read_ahead && amount < config.read_ahead) {
				// A tail window fetches the Parquet trailer and footer together.
				begin = offset >= size - MinValue(size, config.read_ahead)
				            ? size - MinValue(size, config.read_ahead)
				            : offset / config.read_ahead * config.read_ahead;
				end = MinValue(size, MaxValue(end, begin + config.read_ahead));
			}
			auto bytes = Fetch(begin, end - begin);
			memcpy(out, bytes.data() + offset - begin, amount);
			if (bytes.size() <= MAX_CACHE) {
				cache_start = begin;
				cache = std::move(bytes);
			}
			offset += amount;
			out += amount;
			count -= amount;
		}
	}
};

class TiDBFileSystem : public FileSystem {
public:
	string GetName() const override { return "tidbfs"; }
	bool CanHandleFile(const string &path) override { return path.compare(0, 9, "tidbfs://") == 0; }
	bool CanSeek() override { return true; }
	bool OnDiskFile(FileHandle &) override { return false; }
	bool IsManuallySet() override { return true; }
	unique_ptr<FileHandle> OpenFile(const string &path, FileOpenFlags flags, optional_ptr<FileOpener> opener) override {
		Path(path);
		if (!flags.OpenForReading() || flags.OpenForWriting() || flags.CreateFileIfNotExists() ||
		    flags.OverwriteExistingFile() || flags.OpenForAppending()) {
			throw NotImplementedException("tidbfs currently supports read-only analytics files");
		}
		auto config = ReadConfig(opener, path);
		auto version = make_shared_ptr<FileVersion>();
		if (auto context = FileOpener::TryGetClientContext(opener)) {
			version = context->registered_state->GetOrCreate<QueryFiles>("tidbfs.query_files")->Get(config, path);
		}
		auto handle = make_uniq<TiDBHandle>(*this, path, flags, std::move(config), version);
		std::lock_guard<std::mutex> lock(version->mu);
		if (version->initialized) {
			handle->size = version->size;
			handle->mtime = version->mtime;
			handle->etag = version->etag;
			return handle;
		}
		auto r = handle->http.Request(handle->Endpoint(), handle->config.token, true, {}, 0);
		if (r.status == 404 && flags.ReturnNullIfNotExists()) {
			return nullptr;
		}
		CheckStatus(r);
		if (r.Header("x-dat9-isdir") == "true") {
			throw IOException("tidbfs cannot open a directory as a file");
		}
		if (r.Header("x-dat9-content-layout") == "extent") {
			throw NotImplementedException("tidbfs extent layout requires the FUSE client");
		}
		handle->size = idx_t(Number(r.Header("content-length")));
		handle->etag = r.Header("etag");
		if (handle->etag.empty() || handle->etag.compare(0, 2, "W/") == 0 || r.Header("accept-ranges") != "bytes") {
			throw IOException("Upgrade fs: tidbfs requires strong ETags and byte-range reads");
		}
		if (!r.Header("x-dat9-mtime").empty()) {
			handle->mtime = Number(r.Header("x-dat9-mtime"));
		}
		version->size = handle->size;
		version->mtime = handle->mtime;
		version->etag = handle->etag;
		version->initialized = true;
		return handle;
	}
	void Read(FileHandle &base, void *buffer, int64_t count, idx_t offset) override {
		auto &h = base.Cast<TiDBHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0) {
			throw IOException("Negative tidbfs read size");
		}
		h.ReadAt(buffer, idx_t(count), offset);
	}
	int64_t Read(FileHandle &base, void *buffer, int64_t count) override {
		auto &h = base.Cast<TiDBHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		if (count < 0) {
			throw IOException("Negative tidbfs read size");
		}
		auto amount = MinValue<idx_t>(idx_t(count), h.size - MinValue(h.position, h.size));
		h.ReadAt(buffer, amount, MinValue(h.position, h.size));
		h.position += amount;
		return int64_t(amount);
	}
	int64_t GetFileSize(FileHandle &base) override { return int64_t(base.Cast<TiDBHandle>().size); }
	timestamp_t GetLastModifiedTime(FileHandle &base) override {
		auto seconds = base.Cast<TiDBHandle>().mtime;
		if (seconds > std::numeric_limits<int64_t>::max() / 1000000) {
			throw IOException("Invalid tidbfs modification time");
		}
		return timestamp_t(seconds * 1000000);
	}
	string GetVersionTag(FileHandle &base) override { return base.Cast<TiDBHandle>().etag; }
	FileType GetFileType(FileHandle &) override { return FileType::FILE_TYPE_REGULAR; }
	void Seek(FileHandle &base, idx_t position) override {
		auto &h = base.Cast<TiDBHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		h.position = position;
	}
	idx_t SeekPosition(FileHandle &base) override {
		auto &h = base.Cast<TiDBHandle>();
		std::lock_guard<std::mutex> lock(h.mu);
		return h.position;
	}
	bool FileExists(const string &path, optional_ptr<FileOpener> opener) override {
		auto config = ReadConfig(opener, path);
		HTTP http;
		auto r = http.Request(config.endpoint + "/v1/fs" + EncodePath(Path(path)), config.token, true, {}, 0);
		if (r.status == 404) {
			return false;
		}
		CheckStatus(r);
		return r.Header("x-dat9-isdir") != "true";
	}
	vector<OpenFileInfo> Glob(const string &pattern, FileOpener *opener) override {
		auto path = Path(pattern);
		if (!HasGlob(path)) {
			return {OpenFileInfo(pattern)};
		}
		auto parts = StringUtil::Split(path.substr(1), '/');
		HTTP http;
		std::set<string> matches;
		idx_t visited = 0;
		std::function<void(string, idx_t, idx_t)> walk = [&](string directory, idx_t part, idx_t depth) {
			if (depth > 64 || ++visited > 100000) {
				throw IOException("tidbfs glob traversal limit exceeded; use a narrower prefix");
			}
			if (part >= parts.size()) {
				return;
			}
			if (part + 1 < parts.size() && !HasGlob(parts[part])) {
				walk(directory + parts[part] + "/", part + 1, depth + 1);
				return;
			}
			auto config = ReadConfig(opener, "tidbfs://" + directory);
			auto r = http.Request(config.endpoint + "/v1/fs" + EncodePath(directory) + "?list=1", config.token, false, {}, MAX_LIST);
			if (r.status == 404) {
				return;
			}
			CheckStatus(r);
			auto json = nlohmann::json::parse(r.body, nullptr, false);
			if (json.is_discarded() || !json.contains("entries") || !json["entries"].is_array()) {
				throw IOException("Invalid tidbfs directory response");
			}
			bool recursive = parts[part] == "**";
			if (recursive && part + 1 < parts.size()) {
				walk(directory, part + 1, depth);
			}
			for (auto &entry : json["entries"]) {
				if (!entry.contains("name") || !entry["name"].is_string() || !entry.contains("isDir") || !entry["isDir"].is_boolean()) {
					throw IOException("Invalid tidbfs directory entry");
				}
				auto name = entry["name"].get<std::string>();
				bool dir = entry["isDir"].get<bool>();
				if (dir && !name.empty() && name.back() == '/') {
					name.pop_back();
				}
				if (name.empty() || name == "." || name == ".." || name.find_first_of("/\\") != string::npos || name.find('\0') != string::npos) {
					throw IOException("Invalid tidbfs directory entry name");
				}
				if (!recursive && fnmatch(parts[part].c_str(), name.c_str(), 0) != 0) {
					continue;
				}
				if (dir && (recursive || part + 1 < parts.size())) {
					walk(directory + name + "/", recursive ? part : part + 1, depth + 1);
				} else if (!dir && part + 1 == parts.size()) {
					matches.insert("tidbfs://" + directory + name);
					if (matches.size() > 100000) {
						throw IOException("tidbfs glob file limit exceeded");
					}
				}
			}
		};
		walk("/", 0, 0);
		vector<OpenFileInfo> result;
		for (auto &match : matches) {
			result.emplace_back(match);
		}
		return result;
	}
};

unique_ptr<BaseSecret> CreateTiDBSecret(ClientContext &, CreateSecretInput &input) {
	auto scope = input.scope;
	if (scope.empty()) {
		scope.push_back("tidbfs://");
	}
	auto secret = make_uniq<KeyValueSecret>(scope, input.type, input.provider, input.name);
	if (!secret->TrySetValue("endpoint", input) || !secret->TrySetValue("token", input)) {
		throw InvalidInputException("tidbfs secret requires ENDPOINT and TOKEN");
	}
	CheckEndpoint(secret->secret_map["endpoint"].GetValue<string>());
	secret->redact_keys.insert("token");
	return secret;
}

} // namespace

void LoadTiDBFS(ExtensionLoader &loader) {
	static std::once_flag init;
	std::call_once(init, [] {
		if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
			throw IOException("Cannot initialize tidbfs HTTP library");
		}
	});
	SecretType type;
	type.name = "tidbfs";
	type.deserializer = KeyValueSecret::Deserialize<KeyValueSecret>;
	type.default_provider = "config";
	type.extension = "tidbfs";
	loader.RegisterSecretType(type);
	CreateSecretFunction function;
	function.secret_type = "tidbfs";
	function.provider = "config";
	function.function = CreateTiDBSecret;
	function.named_parameters["endpoint"] = LogicalType::VARCHAR;
	function.named_parameters["token"] = LogicalType::VARCHAR;
	loader.RegisterFunction(function);
	DBConfig::GetConfig(loader.GetDatabaseInstance()).AddExtensionOption(
	    "tidbfs_read_ahead", "Bytes to fetch ahead on small reads (0 disables prefetch, max 1 MiB)",
	    LogicalType::BIGINT, Value::BIGINT(65536));
	FileSystem::GetFileSystem(loader.GetDatabaseInstance()).RegisterSubSystem(make_uniq<TiDBFileSystem>());
}

} // namespace duckdb

extern "C" {
DUCKDB_CPP_EXTENSION_ENTRY(tidbfs, loader) {
	duckdb::LoadTiDBFS(loader);
}
}
