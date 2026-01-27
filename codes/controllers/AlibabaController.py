from codes.controllers.ImageController import ImageController
from dotenv import load_dotenv

load_dotenv()


class AlibabaController:
    def __init__(self):
        self.APP_KEY = ""
        self.imageController = ImageController()

    def request_auth(self, app_callback_url):
        url = f"https://openapi-auth.alibaba.com/oauth/authorize?response_type=code&redirect_uri={app_callback_url}&client_id={self.APP_KEY}"
        return url

    def rename_edited_folder_image_by_similarity(self):
        pass
