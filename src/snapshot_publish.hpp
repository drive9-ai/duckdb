// Publish through native checkpoint exclusion, without interpreting DuckDB's
// on-disk headers or changing its database/WAL locking protocol.
struct PublishInfo : TableFunctionInfo {
	std::shared_ptr<ExtentClients> clients;
	explicit PublishInfo(std::shared_ptr<ExtentClients> clients) : clients(std::move(clients)) { }
};
struct PublishBindData : FunctionData {
	string database;
	std::shared_ptr<ExtentClients> clients;
	PublishBindData(string database, std::shared_ptr<ExtentClients> clients)
	    : database(std::move(database)), clients(std::move(clients)) { }
	unique_ptr<FunctionData> Copy() const override { return make_uniq<PublishBindData>(database, clients); }
	bool Equals(const FunctionData &other) const override {
		auto &data = other.Cast<PublishBindData>();
		return database == data.database && clients == data.clients;
	}
};
struct PublishState : GlobalTableFunctionState { bool done = false; };

unique_ptr<FunctionData> PublishBind(ClientContext &, TableFunctionBindInput &input,
                                   vector<LogicalType> &types, vector<Identifier> &names) {
	if (input.inputs[0].IsNull()) throw BinderException("Database cannot be NULL");
	types = {LogicalType::VARCHAR, LogicalType::UBIGINT};
	names = {"reader_uri", "generation"};
	auto &info = input.info->Cast<PublishInfo>();
	return make_uniq<PublishBindData>(input.inputs[0].GetValue<string>(), info.clients);
}
void PublishCheckpoint(ClientContext &context, TableFunctionInput &input, DataChunk &output) {
	auto &state = input.global_state->Cast<PublishState>();
	if (state.done) return;
	state.done = true; // never replay a side effect on another output request
	auto &data = input.bind_data->Cast<PublishBindData>();
	auto db = DatabaseManager::Get(context).GetDatabase(context, Identifier(data.database));
	if (!db) throw BinderException("Database \"%s\" not found", data.database);
	auto &storage = db->GetStorageManager();
	auto path = NormalizeURI(storage.GetDBPath());
	if (db->IsReadOnly() || path.compare(0, 13, "tidbfs+db:///") != 0 ||
	    StringUtil::EndsWith(path, ".wal"))
		throw InvalidInputException("tidbfs_publish requires a writable native tidbfs+db:/// attachment (name must not end in .wal)");
	if (ValidChecker::IsInvalidated(*db)) throw IOException("Database is invalidated; reopen it before publishing");
	auto current = Transaction::TryGet(context, *db);
	if (current && current->Cast<DuckTransaction>().ChangesMade())
		throw TransactionException("Commit or roll back transaction-local changes before tidbfs_publish");
	ClientContextFileOpener opener(context);
	auto client = data.clients->Get(ReadConfig(&opener, path));
	if (!client->fs->CloneSupported()) throw IOException("Server lacks session-guarded extent clone_v1 support");
	auto writer = data.clients->Writer(client, path);
	std::unique_lock<std::mutex> lifetime(writer->mu);
	if (!writer->handle) throw IOException("Native writer handle is not open in this DuckDB instance");
	auto &manager = DuckTransactionManager::Get(*db);
	auto checkpoint_lock = manager.TryGetCheckpointLock();
	if (!checkpoint_lock) throw TransactionException("Cannot publish while another update/catalog transaction or checkpoint is active; retry after it finishes");
	CheckpointOptions options;
	options.action = CheckpointAction::ALWAYS_CHECKPOINT;
	// The concurrent checkpoint path retains versions needed by local readers.
	// Append/delete transactions may continue, exactly as with native CHECKPOINT;
	// their WAL is not part of the published self-contained committed checkpoint.
	options.type = CheckpointType::CONCURRENT_CHECKPOINT;
	storage.CreateCheckpoint(context, options);
	// Keep checkpoint exclusion through the clone, so no later checkpoint can
	// replace the catalog/header between checkpoint completion and publication.
	auto &handle = *writer->handle;
	std::unique_lock<std::shared_mutex> io(handle.mu);
	handle.Check(true);
	try {
		auto target = SnapshotTarget(ExtentPath(path));
		auto previous = client->fs->Stat(target);
		auto published = client->fs->Clone(handle.handle, target, previous.exists ? previous.inode : 0);
		output.SetValue(0, 0, Value("tidbfs+snapshot://" + ExtentPath(path)));
		output.SetValue(1, 0, Value::UBIGINT(published.inode));
		output.SetCardinality(1);
	} catch (const std::exception &error) { throw IOException("tidbfs publish: %s", error.what()); }
}
void RegisterTiDBPublish(ExtensionLoader &loader, const std::shared_ptr<ExtentClients> &clients) {
	TableFunction function("tidbfs_publish", FunctionSignature().AddPositionalOnly("database", LogicalType::VARCHAR),
	                       PublishCheckpoint, PublishBind,
	                       [](ClientContext &, TableFunctionInitInput &) -> unique_ptr<GlobalTableFunctionState> {
		                       return make_uniq<PublishState>();
	                       });
	function.function_info = make_shared_ptr<PublishInfo>(clients);
	loader.RegisterFunction(function);
}
