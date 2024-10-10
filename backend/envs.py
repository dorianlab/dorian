from dask.distributed import Client, LocalCluster, Queue, PipInstall, worker_client
from yacman import FutureYAMLConfigManager as YAMLConfigManager
import redis


worker_client = worker_client
config = YAMLConfigManager.from_yaml_file("config.yaml")
redis = redis.Redis(host=config['redis']['host'], port=config['redis']['port'],
                    username="default", # use your Redis user. More info https://redis.io/docs/management/security/acl/
                    password=config['redis']['api'],
                    decode_responses=True)

cluster = LocalCluster(n_workers=3, threads_per_worker=1, memory_limit='2GB', processes=False)
executor = Client(cluster)
plugin = PipInstall(packages=["scikit-learn", "pandas", "aenum"], pip_options=["--upgrade"])
executor.register_plugin(plugin)
    
queue = Queue('futures')  


def submit(fn, data):
    future = executor.submit(fn, data)
    queue.put(future)
    return future


def close():
    executor.close()
    cluster.close()