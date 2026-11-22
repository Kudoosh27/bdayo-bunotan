import os


bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
timeout = 60
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True
forwarded_allow_ips = os.environ.get("GUNICORN_TRUSTED_PROXIES", "127.0.0.1")
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}
