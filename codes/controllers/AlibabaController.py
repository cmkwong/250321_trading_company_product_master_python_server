
class AlibabaController:
    def __init__(self):
        self.APP_KEY = ''

    def request_auth(self, app_callback_url):
        url = f"https://openapi-auth.alibaba.com/oauth/authorize?response_type=code&redirect_uri={app_callback_url}&client_id={self.APP_KEY}"