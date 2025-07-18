import logging
import sys

def setup_logging(log_file=None, log_level='INFO'):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode='a'))
    level = getattr(logging, log_level.upper(), logging.INFO) if isinstance(log_level, str) else log_level
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s [%(step)s][%(table)s] %(message)s',
        handlers=handlers
    )
    logging.LoggerAdapter(logging.getLogger(), {'step': '', 'table': ''})

def get_logger(step='', table=''):
    return logging.LoggerAdapter(logging.getLogger(), {'step': step, 'table': table}) 