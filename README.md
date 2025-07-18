# Aurora MySQL Database Environment Refresher

This tool automates the process of refreshing a pre-production Aurora MySQL database environment from a production backup. It is modular, secure, and uses AWS best practices.

## Features
- Automated snapshot creation of production Aurora MySQL
- Cross-account snapshot sharing
- Modular workflow for further automation (provisioning, schema refresh, data copy)
- Secure credential management via AWS Secrets Manager
- Configurable via YAML
- Logging and error handling
- **Parallel table copy** for faster data migration
- **DMS integration** for large-scale or production-grade migrations
- **Granular log levels** (DEBUG, INFO, WARNING, ERROR)
- **Automatic retry of only failed tables** for robust error recovery

## Prerequisites
- Python 3.7+
- AWS CLI configured with credentials/roles for both source and target accounts
- IAM permissions for RDS, DMS, Secrets Manager, and S3
- The following Python packages (install with `pip install -r requirements.txt`):
  - boto3
  - PyYAML
  - mysql-connector-python

- For fast table copy: `mysqldump` and `mysql` must be available in your PATH

## Setup
1. **Clone the repository**
2. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```
3. **Configure AWS credentials:**
   - Use `aws configure` or set up environment variables/roles as needed.
4. **Edit `config.yaml`:**
   - Fill in your source/target DB identifiers, AWS account IDs, schema, S3 DDL paths, Secrets Manager ARNs, and new options for parallelism, DMS, logging, and retries.

## Configuration Example (`config.yaml`)
```yaml
source_account_id: '123456789012'
target_account_id: '210987654321'
region: 'us-east-1'
source_db_cluster_identifier: 'prod-aurora-cluster'
target_db_cluster_identifier: 'preprod-aurora-cluster'
backup_db_instance_identifier: 'prod-backup-db'
backup_db:
  host: 'prod-backup-db.cluster-xxxxxx.us-east-1.rds.amazonaws.com'
  port: 3306
  dbname: 'analytics'
target_db:
  host: 'preprod-app-db.cluster-yyyyyy.us-east-1.rds.amazonaws.com'
  port: 3306
  dbname: 'analytics'
schema_to_refresh: 'analytics_schema'
tables_to_copy:
  - table1
  - table2
  - table3
  # or use: all
row_threshold_for_mysqldump: 100000  # tables above this row count use mysqldump
parallel_table_copies: 4  # Number of tables to copy in parallel
# Logging
dms:
  enabled: false
  replication_instance_arn: ''
  source_endpoint_arn: ''
  target_endpoint_arn: ''
  migration_task_settings: ''  # Path to JSON file with DMS task settings (optional)
log_level: INFO  # DEBUG, INFO, WARNING, ERROR
max_failed_table_retries: 2
s3_ddl_bucket: 'my-ddl-bucket'
s3_ddl_prefix: 'ddls/analytics_schema.sql'
subnet_group: 'my-db-subnet-group'
vpc_security_group_ids:
  - 'sg-0123456789abcdef0'
source_db_secret_arn: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db-secret'
target_db_secret_arn: 'arn:aws:secretsmanager:us-east-1:210987654321:secret:preprod-db-secret'
```

## Usage

### Step 1: Backup Production Database & Share Snapshot
Creates a snapshot of the production Aurora cluster and shares it with the preprod account.

```sh
python main.py --step step1
```

### Step 2: Provision Backup-Based Database
Restores a new Aurora instance in preprod from the shared snapshot.

```sh
python main.py --step step2
```

### Step 3: Modify Target Database
Drops and recreates the specified schema using DDL from S3.

```sh
python main.py --step step3
```

### Step 4: Copy Data (Parallel, Efficient, or DMS)
Copies all (or specified) tables from the backup DB to the target DB. Uses parallelism and `mysqldump` for large tables by default. Failed tables are automatically retried up to the configured limit.

```sh
python main.py --step step4
```

- To override tables to copy:
  ```sh
  python main.py --step step4 --tables table1 table2
  ```
- To control parallelism, set `parallel_table_copies` in `config.yaml`.
- To use DMS for all table copies, set `dms.enabled: true` and fill in the DMS ARNs in `config.yaml`.
- To set log level, use `log_level` in `config.yaml` or override with CLI:
  ```sh
  python main.py --log-level DEBUG
  ```
- To log to a file:
  ```sh
  python main.py --log-file my-refresh.log
  ```

## Parallel Table Copy
- The tool uses Python's `ThreadPoolExecutor` to copy multiple tables in parallel (default: 4 workers, configurable).
- Large tables (above `row_threshold_for_mysqldump`) are copied using `mysqldump`/`mysql` for speed.

## DMS Integration
- If `dms.enabled` is set to `true`, AWS Database Migration Service is used for all table copies.
- You must provide the DMS replication instance ARN, source/target endpoint ARNs, and (optionally) a migration task settings JSON file.
- The tool will create and start a DMS full-load task and wait for completion.

## Logging
- Logs are printed to the terminal and can also be written to a file.
- Log level is configurable (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- All logs include step and table context for easy troubleshooting.

## Error Recovery & Retries
- If any tables fail to copy, only those tables are retried, up to `max_failed_table_retries` times (configurable).
- All errors and retries are logged with context.
- At the end of each run, a summary of successes and failures is printed.

## Security
- All DB credentials are stored in AWS Secrets Manager (never hardcoded).
- Ensure your IAM roles have least-privilege permissions for RDS, DMS, S3, and Secrets Manager.

## Extending
- Each step is modular and can be run independently or as part of a full workflow.
- Add your own logic for schema/data operations in the respective modules.

## License
MIT 