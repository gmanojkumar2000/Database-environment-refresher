import mysql.connector
import logging
import boto3
import base64
import json
import subprocess
import tempfile
import os
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time
from modules.logger import get_logger

def get_db_connection(secret_arn, region, host=None, port=None, dbname=None):
    """
    Get a MySQL connection using credentials from AWS Secrets Manager.
    Optionally override host/port/dbname.
    """
    logger = logging.getLogger(__name__)
    session = boto3.session.Session()
    client = session.client(service_name='secretsmanager', region_name=region)
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_arn)
        secret = get_secret_value_response['SecretString']
        creds = json.loads(secret)
        conn = mysql.connector.connect(
            host=host or creds['host'],
            user=creds['username'],
            password=creds['password'],
            database=dbname or creds.get('dbname'),
            port=int(port or creds.get('port', 3306))
        )
        logger.info(f"Connected to MySQL at {host or creds['host']}")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to DB: {e}")
        raise

def get_table_row_count(conn, schema, table):
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM `{schema}`.`{table}`;")
    count = cursor.fetchone()[0]
    cursor.close()
    return count

def drop_schema(conn, schema):
    """
    Drop the specified schema and all its tables.
    """
    logger = logging.getLogger(__name__)
    try:
        cursor = conn.cursor()
        cursor.execute(f"DROP SCHEMA IF EXISTS `{schema}`;")
        logger.info(f"Dropped schema {schema}.")
        cursor.close()
    except Exception as e:
        logger.error(f"Failed to drop schema {schema}: {e}")
        raise

def recreate_schema(conn, ddl_sql):
    """
    Recreate the schema using provided DDL.
    """
    logger = logging.getLogger(__name__)
    try:
        cursor = conn.cursor()
        for stmt in ddl_sql.split(';'):
            if stmt.strip():
                cursor.execute(stmt)
        logger.info("Schema recreated from DDL.")
        cursor.close()
    except Exception as e:
        logger.error(f"Failed to recreate schema: {e}")
        raise

def retry(func, max_attempts=3, initial_delay=2, backoff=2, logger=None):
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            if logger:
                logger.warning(f"Attempt {attempt} failed: {e}")
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay *= backoff

def copy_data(src_conn, tgt_conn, schema, tables=None, config=None, src_secret=None, tgt_secret=None, region=None):
    logger = get_logger(step='step4')
    dms_cfg = config.get('dms', {}) if config else {}
    failed_tables = []
    if dms_cfg.get('enabled'):
        logger.info("DMS is enabled. Using DMS for all table copies.")
        try:
            retry(lambda: dms_copy_tables(schema, tables, config, dms_cfg, region), logger=logger)
        except Exception as e:
            logger.error(f"DMS copy failed: {e}")
            return tables if tables != 'all' else ['all']
        return []
    parallelism = config.get('parallel_table_copies', 1)
    threshold = config.get('row_threshold_for_mysqldump', 100000)
    if tables is None or tables == 'all':
        src_cursor = src_conn.cursor()
        src_cursor.execute(f"SHOW TABLES IN `{schema}`;")
        tables = [row[0] for row in src_cursor.fetchall()]
        src_cursor.close()
    total = len(tables)
    completed = 0
    progress_logger = get_logger(step='step4', table='progress')
    def copy_table(table):
        table_logger = get_logger(step='step4', table=table)
        def do_copy():
            row_count = get_table_row_count(src_conn, schema, table)
            table_logger.info(f"Table {table} has {row_count} rows.")
            if row_count >= threshold:
                table_logger.info(f"Using mysqldump for table {table}...")
                mysqldump_table(schema, table, config, src_secret, tgt_secret, region)
            else:
                table_logger.info(f"Copying table {table} row-by-row...")
                src_cursor = src_conn.cursor()
                tgt_cursor = tgt_conn.cursor()
                src_cursor.execute(f"SELECT * FROM `{schema}`.`{table}`;")
                rows = src_cursor.fetchall()
                if rows:
                    columns = [desc[0] for desc in src_cursor.description]
                    placeholders = ','.join(['%s'] * len(columns))
                    insert_sql = f"INSERT INTO `{schema}`.`{table}` ({', '.join(columns)}) VALUES ({placeholders})"
                    tgt_cursor.executemany(insert_sql, rows)
                    tgt_conn.commit()
                    table_logger.info(f"Copied {len(rows)} rows to {table}.")
                src_cursor.close()
                tgt_cursor.close()
        try:
            retry(do_copy, logger=table_logger)
            return (table, 'success')
        except Exception as e:
            table_logger.error(f"Failed to copy table {table}: {e}")
            return (table, 'error')
    logger.info(f"Copying {total} tables in parallel with {parallelism} workers...")
    results = []
    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {executor.submit(copy_table, table): table for table in tables}
        for i, future in enumerate(as_completed(futures), 1):
            table, result = future.result()
            results.append((table, result))
            completed += 1
            progress_logger.info(f"Progress: {completed}/{total} tables done.")
    failed_tables = [table for table, result in results if result != 'success']
    logger.info(f"Table copy summary: {total - len(failed_tables)} succeeded, {len(failed_tables)} failed.")
    if failed_tables:
        logger.warning(f"Failed tables: {failed_tables}")
    return failed_tables

def mysqldump_table(schema, table, config, src_secret, tgt_secret, region):
    """
    Use mysqldump and mysql CLI to copy a table from source to target DB.
    """
    logger = logging.getLogger(__name__)
    # Get credentials for both DBs
    session = boto3.session.Session()
    client = session.client(service_name='secretsmanager', region_name=region)
    src_creds = json.loads(client.get_secret_value(SecretId=src_secret)['SecretString'])
    tgt_creds = json.loads(client.get_secret_value(SecretId=tgt_secret)['SecretString'])
    backup_db = config['backup_db']
    target_db = config['target_db']
    # Dump table to temp file
    with tempfile.NamedTemporaryFile(delete=False) as tmpfile:
        dump_cmd = [
            'mysqldump',
            f"-h{backup_db['host']}",
            f"-P{backup_db['port']}",
            f"-u{src_creds['username']}",
            f"-p{src_creds['password']}",
            '--single-transaction',
            '--skip-lock-tables',
            backup_db['dbname'],
            table
        ]
        logger.info(f"Running: {' '.join(dump_cmd[:-1])} [table omitted]")
        subprocess.run(dump_cmd, stdout=tmpfile, check=True)
        tmpfile_path = tmpfile.name
    # Load table into target
    load_cmd = [
        'mysql',
        f"-h{target_db['host']}",
        f"-P{target_db['port']}",
        f"-u{tgt_creds['username']}",
        f"-p{tgt_creds['password']}",
        target_db['dbname']
    ]
    logger.info(f"Loading table {table} into target DB...")
    with open(tmpfile_path, 'rb') as infile:
        subprocess.run(load_cmd, stdin=infile, check=True)
    os.remove(tmpfile_path)
    logger.info(f"mysqldump/mysql copy for table {table} complete.")

def dms_copy_tables(schema, tables, config, dms_cfg, region):
    logger = get_logger(step='step4', table='DMS')
    dms = boto3.client('dms', region_name=region)
    task_id = f"refresh-{schema}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    table_mappings = {
        "rules": [
            {
                "rule-type": "selection",
                "rule-id": "1",
                "rule-name": "1",
                "object-locator": {
                    "schema-name": schema,
                    "table-name": table if tables != 'all' else '%'
                },
                "rule-action": "include"
            } for table in (tables if tables != 'all' else ['%'])
        ]
    }
    settings = dms_cfg.get('migration_task_settings')
    if settings:
        with open(settings, 'r') as f:
            task_settings = json.load(f)
    else:
        task_settings = {}
    logger.info(f"Creating DMS task {task_id} for tables: {tables}")
    resp = dms.create_replication_task(
        ReplicationTaskIdentifier=task_id,
        SourceEndpointArn=dms_cfg['source_endpoint_arn'],
        TargetEndpointArn=dms_cfg['target_endpoint_arn'],
        ReplicationInstanceArn=dms_cfg['replication_instance_arn'],
        MigrationType='full-load',
        TableMappings=json.dumps(table_mappings),
        ReplicationTaskSettings=json.dumps(task_settings) if task_settings else '{}'
    )
    dms_task_arn = resp['ReplicationTask']['ReplicationTaskArn']
    logger.info(f"Starting DMS task {task_id}...")
    dms.start_replication_task(
        ReplicationTaskArn=dms_task_arn,
        StartReplicationTaskType='start-replication'
    )
    # Wait for task to complete
    waiter = dms.get_waiter('replication_task_stopped')
    logger.info(f"Waiting for DMS task {task_id} to complete...")
    waiter.wait(Filters=[{'Name': 'replication-task-arn', 'Values': [dms_task_arn]}])
    logger.info(f"DMS task {task_id} completed.") 