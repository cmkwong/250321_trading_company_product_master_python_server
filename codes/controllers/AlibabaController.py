import os
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

    def _rename_images_by_similarity(self, original_folder_path, edited_folder_path):
        """Rename images in edited folder to match similar images in original folder."""
        # loop through edited folder images
        for img_name in os.listdir(edited_folder_path):
            edited_img_path = os.path.join(edited_folder_path, img_name)
            best_similarity = 0
            best_match_name = ""
            # compare with original folder images
            for orig_img_name in os.listdir(original_folder_path):
                orig_img_path = os.path.join(original_folder_path, orig_img_name)
                similarity = self.imageController.compare_image_similarity(
                    orig_img_path, edited_img_path
                )
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match_name = orig_img_name
            # rename edited image to match the best original image name
            if best_match_name:
                new_edited_img_path = os.path.join(edited_folder_path, best_match_name)
                # handle duplicate names by adding counter
                if os.path.exists(new_edited_img_path):
                    filename, ext = os.path.splitext(best_match_name)
                    counter = 2
                    while os.path.exists(new_edited_img_path):
                        new_name = f"{filename}_{counter}{ext}"
                        new_edited_img_path = os.path.join(edited_folder_path, new_name)
                        counter += 1
                os.rename(edited_img_path, new_edited_img_path)

    def rename_edited_folder_image_by_similarity(self, *, product_folder_name=""):
        # display folder path
        display_folder_path = os.path.join(
            os.getenv("PRODUCT_FOLDER_PATH"), product_folder_name, "display"
        )
        edited_display_folder_path = os.path.join(
            os.getenv("PRODUCT_FOLDER_PATH"), product_folder_name, "edited/display"
        )
        self._rename_images_by_similarity(
            display_folder_path, edited_display_folder_path
        )

        # description folder path
        description_folder_path = os.path.join(
            os.getenv("PRODUCT_FOLDER_PATH"), product_folder_name, "description"
        )
        edited_description_folder_path = os.path.join(
            os.getenv("PRODUCT_FOLDER_PATH"), product_folder_name, "edited/description"
        )
        self._rename_images_by_similarity(
            description_folder_path, edited_description_folder_path
        )
