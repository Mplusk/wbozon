# Example only: PORT must be chosen after checking the shared server.
import os
bind = '127.0.0.1:' + os.environ.get('PORT','18081')
workers = 2
timeout = 30
accesslog = None  # Never log magic-link query strings.
errorlog = '-'
capture_output = False
limit_request_line = 2048
limit_request_fields = 30
limit_request_field_size = 4096
