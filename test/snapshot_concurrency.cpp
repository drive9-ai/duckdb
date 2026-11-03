// Real DuckDB connections exercise checkpoint exclusion and concurrent append
// visibility. Independent-process attachment/failure cases live in snapshot_smoke.py.
#include "duckdb.h"
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

std::string Quote(const std::string &s) {
    std::string out = "'";
    for (char c : s) { out += c; if (c == '\'') out += c; }
    return out + "'";
}
struct Result {
    duckdb_result value{};
    ~Result() { duckdb_destroy_result(&value); }
};
struct Database {
    duckdb_database db = nullptr;
    Database() {
        duckdb_config config = nullptr;
        if (duckdb_create_config(&config) != DuckDBSuccess) throw std::runtime_error("config failed");
        duckdb_set_config(config, "allow_unsigned_extensions", "true");
        char *error = nullptr;
        auto state = duckdb_open_ext(nullptr, &db, config, &error);
        duckdb_destroy_config(&config);
        std::string message = error ? error : "open failed";
        duckdb_free(error);
        if (state != DuckDBSuccess) throw std::runtime_error(message);
    }
    ~Database() { duckdb_close(&db); }
};
struct Connection {
    duckdb_connection connection = nullptr;
    explicit Connection(Database &db) {
        if (duckdb_connect(db.db, &connection) != DuckDBSuccess) throw std::runtime_error("connect failed");
    }
    ~Connection() { duckdb_disconnect(&connection); }
    std::string Try(const std::string &sql) {
        Result result;
        if (duckdb_query(connection, sql.c_str(), &result.value) != DuckDBSuccess)
            return duckdb_result_error(&result.value);
        return "";
    }
    void Run(const std::string &sql) { auto error = Try(sql); if (!error.empty()) throw std::runtime_error(error); }
    int64_t Number(const std::string &sql) {
        Result result;
        if (duckdb_query(connection, sql.c_str(), &result.value) != DuckDBSuccess)
            throw std::runtime_error(duckdb_result_error(&result.value));
        return duckdb_value_int64(&result.value, 0, 0);
    }
};

int main(int argc, char **argv) {
    const char *token = std::getenv("DRIVE9_API_KEY"), *endpoint = std::getenv("DRIVE9_BASE");
    if (argc != 2 || !token || !endpoint) return 2;
    try {
        std::string init = "LOAD " + Quote(argv[1]) + "; CREATE SECRET fs (TYPE tidbfs, ENDPOINT " + Quote(endpoint) + ", TOKEN " + Quote(token) + "); ";
        std::string path = "/concurrency-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) + ".duckdb";
        Database writer;
        Connection write(writer), publish(writer);
        write.Run(init + "ATTACH " + Quote("tidbfs+db://" + path) + " AS remote; CREATE TABLE remote.t AS SELECT i id, 0 epoch FROM range(1000) t(i); CREATE TABLE remote.version AS SELECT 0 epoch; CALL tidbfs_publish('remote');");
        auto read = [&] (const std::string &query) {
            Database reader;
            Connection c(reader);
            c.Run(init + "ATTACH " + Quote("tidbfs+snapshot://" + path) + " AS remote (READ_ONLY);");
            return c.Number(query);
        };
        write.Run("BEGIN; UPDATE remote.t SET epoch=1;");
        auto blocked = publish.Try("CALL tidbfs_publish('remote')");
        if (blocked.find("Cannot publish") == std::string::npos) throw std::runtime_error("publication did not reject an active update transaction: " + blocked);
        write.Run("ROLLBACK");
        write.Run("BEGIN; INSERT INTO remote.t VALUES (1000, 0); DELETE FROM remote.t WHERE id=0;");
        publish.Run("CALL tidbfs_publish('remote')");
        if (read("SELECT sum(id) FROM remote.t") != 499500) throw std::runtime_error("checkpoint exposed uncommitted append/delete");
        write.Run("COMMIT");
        publish.Run("CALL tidbfs_publish('remote')");
        if (read("SELECT sum(id) FROM remote.t") != 500500) throw std::runtime_error("checkpoint omitted committed append/delete");
        std::atomic<bool> finished{false};
        std::exception_ptr failure;
        std::thread updating([&] {
            try {
                for (int i = 1; i <= 16; ++i) {
                    write.Run("BEGIN; UPDATE remote.t SET epoch=" + std::to_string(i) + "; UPDATE remote.version SET epoch=" + std::to_string(i) + "; COMMIT;");
                }
            } catch (...) { failure = std::current_exception(); }
            finished = true;
        });
        int published = 0;
        try {
            do {
                auto error = publish.Try("CALL tidbfs_publish('remote')");
                if (!error.empty()) {
                    if (error.find("Cannot publish") == std::string::npos) throw std::runtime_error(error);
                    std::this_thread::yield();
                    continue;
                }
                ++published;
                if (read("SELECT CASE WHEN count(*)=1000 AND min(t.epoch)=max(t.epoch) AND min(t.epoch)=min(v.epoch) THEN 1 ELSE 0 END FROM remote.t t CROSS JOIN remote.version v") != 1)
                    throw std::runtime_error("reader mixed transactional generations across tables");
            } while (!finished || published < 2);
        } catch (...) { updating.join(); throw; }
        updating.join();
        if (failure) std::rethrow_exception(failure);
        publish.Run("CALL tidbfs_publish('remote')");
        if (read("SELECT min(epoch) FROM remote.t") != 16) throw std::runtime_error("final generation missing");
        std::cout << "PASS: active updates excluded, uncommitted append/delete hidden, concurrent publications are consistent\n";
    } catch (const std::exception &error) {
        std::string message = error.what();
        for (size_t at; (at = message.find(token)) != std::string::npos;) message.replace(at, std::string(token).size(), "<redacted>");
        std::cerr << message << '\n';
        return 1;
    }
}
