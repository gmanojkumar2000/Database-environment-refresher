import boto3
import logging
import time
from botocore.exceptions import ClientError

def create_db_snapshot(cluster_id, snapshot_id, region):
    """
    Create a snapshot of the given Aurora cluster.
    Waits for snapshot to be available.
    Returns the snapshot ARN.
    """
    rds = boto3.client('rds', region_name=region)
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"Creating snapshot {snapshot_id} for cluster {cluster_id}...")
        rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot_id,
            DBClusterIdentifier=cluster_id
        )
    except ClientError as e:
        if 'DBClusterSnapshotAlreadyExistsFault' in str(e):
            logger.warning(f"Snapshot {snapshot_id} already exists. Proceeding.")
        else:
            logger.error(f"Failed to create snapshot: {e}")
            raise
    # Wait for snapshot to be available
    logger.info(f"Waiting for snapshot {snapshot_id} to become available...")
    waiter = rds.get_waiter('db_cluster_snapshot_available')
    try:
        waiter.wait(DBClusterSnapshotIdentifier=snapshot_id)
    except Exception as e:
        logger.error(f"Error waiting for snapshot: {e}")
        raise
    # Get snapshot ARN
    resp = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=snapshot_id)
    arn = resp['DBClusterSnapshots'][0]['DBClusterSnapshotArn']
    logger.info(f"Snapshot {snapshot_id} is available. ARN: {arn}")
    return arn

def share_db_snapshot(snapshot_id, target_account_id, region):
    """
    Share the snapshot with another AWS account using modify_db_cluster_snapshot_attribute.
    """
    rds = boto3.client('rds', region_name=region)
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"Sharing snapshot {snapshot_id} with account {target_account_id}...")
        rds.modify_db_cluster_snapshot_attribute(
            DBClusterSnapshotIdentifier=snapshot_id,
            AttributeName='restore',
            ValuesToAdd=[target_account_id]
        )
        logger.info(f"Snapshot {snapshot_id} shared with account {target_account_id}.")
    except ClientError as e:
        logger.error(f"Failed to share snapshot: {e}")
        raise

def find_latest_shared_snapshot(cluster_id, region, owner_account_id):
    """
    Find the latest available shared snapshot for the given cluster.
    Returns the snapshot identifier.
    """
    rds = boto3.client('rds', region_name=region)
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"Looking for latest shared snapshot for cluster {cluster_id}...")
        resp = rds.describe_db_cluster_snapshots(
            DBClusterIdentifier=cluster_id,
            SnapshotType='shared',
            IncludeShared=True,
            MaxRecords=50
        )
        snapshots = [s for s in resp['DBClusterSnapshots'] if s['Status'] == 'available']
        if not snapshots:
            logger.error("No available shared snapshots found.")
            return None
        # Sort by snapshot create time descending
        snapshots.sort(key=lambda s: s['SnapshotCreateTime'], reverse=True)
        latest = snapshots[0]
        logger.info(f"Latest shared snapshot: {latest['DBClusterSnapshotIdentifier']}")
        return latest['DBClusterSnapshotIdentifier']
    except Exception as e:
        logger.error(f"Error finding shared snapshot: {e}")
        raise

def restore_db_from_snapshot(snapshot_id, new_cluster_id, region, subnet_group=None, vpc_security_group_ids=None):
    """
    Restore a new Aurora cluster from the given snapshot.
    Waits for the cluster to become available.
    Returns the new cluster ARN.
    """
    rds = boto3.client('rds', region_name=region)
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"Restoring new cluster {new_cluster_id} from snapshot {snapshot_id}...")
        params = {
            'DBClusterIdentifier': new_cluster_id,
            'SnapshotIdentifier': snapshot_id,
            'Engine': 'aurora-mysql',
        }
        if subnet_group:
            params['DBSubnetGroupName'] = subnet_group
        if vpc_security_group_ids:
            params['VpcSecurityGroupIds'] = vpc_security_group_ids
        rds.restore_db_cluster_from_snapshot(**params)
    except ClientError as e:
        if 'DBClusterAlreadyExistsFault' in str(e):
            logger.warning(f"Cluster {new_cluster_id} already exists. Proceeding.")
        else:
            logger.error(f"Failed to restore cluster: {e}")
            raise
    # Wait for cluster to be available
    logger.info(f"Waiting for cluster {new_cluster_id} to become available...")
    waiter = rds.get_waiter('db_cluster_available')
    try:
        waiter.wait(DBClusterIdentifier=new_cluster_id)
    except Exception as e:
        logger.error(f"Error waiting for cluster: {e}")
        raise
    resp = rds.describe_db_clusters(DBClusterIdentifier=new_cluster_id)
    arn = resp['DBClusters'][0]['DBClusterArn']
    logger.info(f"Cluster {new_cluster_id} is available. ARN: {arn}")
    return arn 