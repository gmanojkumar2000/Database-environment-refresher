import argparse
import yaml
import sys
from datetime import datetime
from modules import aws_utils, db_utils, ddl_utils, logger


def load_config(path='config.yaml'):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def main():
    parser = argparse.ArgumentParser(description='Aurora MySQL Refresh Automation')
    parser.add_argument('--step', type=str, default='all', help='Step to run (all, step1, step2, step3, step4)')
    parser.add_argument('--snapshot-name', type=str, help='Optional snapshot name')
    parser.add_argument('--subnet-group', type=str, help='DB subnet group for restore (optional)')
    parser.add_argument('--vpc-sg', type=str, nargs='*', help='VPC security group IDs for restore (optional)')
    parser.add_argument('--tables', type=str, nargs='*', help='List of tables to copy (optional, default: config)')
    parser.add_argument('--log-file', type=str, help='Path to log file (optional)')
    parser.add_argument('--log-level', type=str, help='Log level (DEBUG, INFO, WARNING, ERROR)')
    args = parser.parse_args()

    config = load_config()
    log_level = args.log_level or config.get('log_level', 'INFO')
    logger.setup_logging(args.log_file, log_level)

    region = config['region']
    src_cluster = config['source_db_cluster_identifier']
    tgt_account = config['target_account_id']
    backup_cluster = config['backup_db_instance_identifier']
    schema = config['schema_to_refresh']
    s3_bucket = config['s3_ddl_bucket']
    s3_key = config['s3_ddl_prefix']
    src_secret = config['source_db_secret_arn']
    tgt_secret = config['target_db_secret_arn']
    backup_db = config['backup_db']
    target_db = config['target_db']
    tables_to_copy = args.tables if args.tables else config.get('tables_to_copy', 'all')
    max_failed_table_retries = config.get('max_failed_table_retries', 0)

    step_summary = {}

    # Step 1: Backup Production Database
    if args.step in ('all', 'step1'):
        try:
            log = logger.get_logger(step='step1')
            snap_name = args.snapshot_name or f"{src_cluster}-refresh-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            log.info(f"Creating snapshot: {snap_name}")
            snap_arn = aws_utils.create_db_snapshot(src_cluster, snap_name, region)
            log.info(f"Sharing snapshot {snap_name} with account {tgt_account}")
            aws_utils.share_db_snapshot(snap_name, tgt_account, region)
            log.info(f"Snapshot {snap_name} created and shared successfully.")
            step_summary['step1'] = 'success'
        except Exception as e:
            log.error(f"[Step 1] Error: {e}")
            step_summary['step1'] = f'error: {e}'
            sys.exit(1)

    # Step 2: Provision Backup-Based Database
    if args.step in ('all', 'step2'):
        try:
            log = logger.get_logger(step='step2')
            log.info(f"Finding latest shared snapshot for cluster {src_cluster}...")
            latest_snapshot = aws_utils.find_latest_shared_snapshot(src_cluster, region, tgt_account)
            if not latest_snapshot:
                log.error("No shared snapshot found. Exiting.")
                step_summary['step2'] = 'no snapshot found'
                sys.exit(1)
            log.info(f"Restoring new cluster {backup_cluster} from snapshot {latest_snapshot}...")
            subnet_group = args.subnet_group or config.get('subnet_group')
            vpc_sgs = args.vpc_sg or config.get('vpc_security_group_ids')
            cluster_arn = aws_utils.restore_db_from_snapshot(
                latest_snapshot, backup_cluster, region, subnet_group, vpc_sgs
            )
            log.info(f"Cluster {backup_cluster} restored and available. ARN: {cluster_arn}")
            step_summary['step2'] = 'success'
        except Exception as e:
            log.error(f"[Step 2] Error: {e}")
            step_summary['step2'] = f'error: {e}'
            sys.exit(1)

    # Step 3: Modify Target Database
    if args.step in ('all', 'step3'):
        try:
            log = logger.get_logger(step='step3')
            log.info(f"Connecting to target DB to drop and recreate schema {schema}...")
            tgt_conn = db_utils.get_db_connection(
                tgt_secret, region,
                host=target_db['host'], port=target_db['port'], dbname=target_db['dbname']
            )
            db_utils.drop_schema(tgt_conn, schema)
            ddl_sql = ddl_utils.fetch_ddl_from_s3(s3_bucket, s3_key, region)
            db_utils.recreate_schema(tgt_conn, ddl_sql)
            tgt_conn.close()
            log.info(f"Schema {schema} dropped and recreated in target DB.")
            step_summary['step3'] = 'success'
        except Exception as e:
            log.error(f"[Step 3] Error: {e}")
            step_summary['step3'] = f'error: {e}'
            sys.exit(1)

    # Step 4: Copy Data to Environment DB
    if args.step in ('all', 'step4'):
        try:
            log = logger.get_logger(step='step4')
            log.info(f"Connecting to backup and target DBs...")
            src_conn = db_utils.get_db_connection(
                src_secret, region,
                host=backup_db['host'], port=backup_db['port'], dbname=backup_db['dbname']
            )
            tgt_conn = db_utils.get_db_connection(
                tgt_secret, region,
                host=target_db['host'], port=target_db['port'], dbname=target_db['dbname']
            )
            failed_tables = db_utils.copy_data(
                src_conn, tgt_conn, schema, tables_to_copy,
                config=config, src_secret=src_secret, tgt_secret=tgt_secret, region=region
            )
            retry_count = 0
            while failed_tables and retry_count < max_failed_table_retries:
                retry_count += 1
                log.warning(f"Retrying failed tables (attempt {retry_count})...")
                failed_tables = db_utils.copy_data(
                    src_conn, tgt_conn, schema, failed_tables,
                    config=config, src_secret=src_secret, tgt_secret=tgt_secret, region=region
                )
            src_conn.close()
            tgt_conn.close()
            if failed_tables:
                log.error(f"Failed tables after retries: {failed_tables}")
                step_summary['step4'] = f'partial failure: {failed_tables}'
            else:
                log.info(f"Data copied from backup DB to target DB for schema {schema}.")
                step_summary['step4'] = 'success'
        except Exception as e:
            log.error(f"[Step 4] Error: {e}")
            step_summary['step4'] = f'error: {e}'
            sys.exit(1)

    print("\n==== Step Summary ====")
    for step, result in step_summary.items():
        print(f"{step}: {result}")

if __name__ == '__main__':
    main() 