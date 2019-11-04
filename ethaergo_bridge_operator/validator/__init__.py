import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

file_formatter = logging.Formatter(
    '{"level": "%(levelname)s", "time": "%(asctime)s", '
    '"service": "%(funcName)s", "message": %(message)s'
)
stream_formatter = logging.Formatter('%(message)s')


root_dir = os.path.dirname(os.path.dirname(__file__))
file_handler = logging.FileHandler(root_dir + '/logs/validator.log')
file_handler.setFormatter(file_formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(stream_formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)