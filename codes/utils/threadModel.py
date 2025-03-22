import os
import threading

# threading to running a function
def start(job_func, **kwargs):
    print(f"Running thread on func - {job_func.__name__}")
    job_thread = threading.Thread(target=job_func, kwargs=kwargs)
    job_thread.start()