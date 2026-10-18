// Generic file-volume HTTP client. No database filename or sidecar policy.
// Included inside the extension implementation namespace after HTTP helpers.
nlohmann::json FileVolumeRPC(const Config &config, nlohmann::json request) {
	HTTP http;
	auto response = PostJSON(http, config, "/v1/file-volumes", request);
	if (response.status == 501)
		throw IOException("Enable experimental file volumes on a local-provider fs server");
	if (response.status == 409)
		throw IOException("Remote file volume is locked or the file already exists");
	if (response.status == 410)
		throw IOException("Remote file volume lease expired; reacquire the volume");
	auto result = JSON(response);
	if (JSONNumber(result, "version") != 1)
		throw IOException("Unsupported file volume protocol version");
	return result;
}

struct FileVolumeLease {
	Config config;
	string volume, token;
	bool read_only;
	std::mutex mu;
	std::condition_variable cv;
	bool stopped = false, failed = false;
	std::thread heartbeat;
	FileVolumeLease(Config cfg, string path, bool ro, bool create)
	    : config(std::move(cfg)), volume(std::move(path)), read_only(ro) {
		auto result =
		    FileVolumeRPC(config, {{"op", "acquire"}, {"volume", volume}, {"read_only", ro}, {"create", create}});
		token = JSONString(result, "token");
		if (JSONNumber(result, "lease_ms") != 30000)
			throw IOException("Unsupported file volume lease duration");
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
	~FileVolumeLease() {
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
			FileVolumeRPC(config, {{"op", "release"}, {"volume", volume}, {"token", token}});
		} catch (...) {
		}
	}
	nlohmann::json Call(nlohmann::json request) {
		std::lock_guard<std::mutex> lock(mu);
		if (failed)
			throw IOException("Remote file volume session failed; reacquire and "
			                  "reconcile (do not retry a mutation)");
		request["volume"] = volume;
		request["token"] = token;
		try {
			return FileVolumeRPC(config, std::move(request));
		} catch (...) {
			failed = true;
			throw;
		}
	}
};
