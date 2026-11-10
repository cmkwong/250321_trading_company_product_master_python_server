import io
import os

import requests
import base64
import cv2
from skimage import metrics
from PIL import Image
from pathlib import Path

from apps.core import config
from apps.core.services.chat_service import ChatController
from codes.utils import fileModel


class ImageController:
    def __init__(self):
        # self.API_URL = "https://api.deepseek.com/v1/ocr"
        self.API_URL = r"https://www.imagetotext.info/api/imageToText"
        self.chatController = ChatController()

    def image_to_base64(self, image_folder, image_name):
        """
        Helper method to convert an image file to base64 encoded string with data URI

        Args:
            image_path (str): Path to the image file

        Returns:
            str: Base64 encoded image with data URI prefix
        """
        image_path = os.path.join(image_folder, image_name)
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
            extension = Path(image_path).suffix[1:]  # Get file extension without dot
            return f"data:image/{extension};base64,{encoded_string}"

    def extract_text_with_image2text(self, image_folder, image_name):

        # Encode image in base64
        encoded_image = self.image_to_base64(image_folder, image_name)

        # Prepare headers and payload
        headers = {
            "Authorization": f"Bearer {config.get('IMAGETOTEXT_API_KEY')}",
            "Content-Type": "application/json",
        }

        payload = {
            "base64": encoded_image,
        }

        try:
            # Make API request
            response = requests.post(self.API_URL, headers=headers, json=payload)
            response.raise_for_status()

            # Parse response
            result = response.json()
            txt = result.get("result", "No text found")
            txt = txt.replace("<br />", "").replace("\n\n", "\n")
            return txt

        except requests.exceptions.RequestException as e:
            return f"API Error: {str(e)}"
        except Exception as e:
            return f"Error: {str(e)}"

    def write_extracted_txt(self, product_index, sub_prj_folder="display"):
        """
        Using Image2Text API to extract the text from image
        """
        folder = os.path.join(
            config.get("PRODUCT_FOLDER_PATH"), product_index, sub_prj_folder
        )
        images = fileModel.getFileList(folder)
        txt = ""
        for img in images:
            extracted_txt = self.extract_text_with_image2text(folder, img)
            extracted_txt = f"---- {img} ----\n{extracted_txt}\n"
            txt += extracted_txt
        fileModel.write_txt(folder, f"{sub_prj_folder}_{product_index}.txt", txt, "w")
        translated_txt = self.chatController.translate_content_simple(
            txt, "Chinese", "English"
        )
        fileModel.write_txt(
            folder,
            f"{sub_prj_folder}_{product_index}_translated.txt",
            f"{translated_txt}\n",
            "w",
        )

    def getting_bbox(self):
        # init the OCR model (heavy import kept lazy so the web process stays light)
        from paddleocr import PaddleOCR

        ocr = PaddleOCR(use_angle_cls=True, lang="ch")

    def gettingImageSize(self, folderPath, fileName):
        try:
            image_path = os.path.join(folderPath, fileName)
            with Image.open(image_path) as img:
                width, height = img.size
        except FileNotFoundError:
            return None, None
        except Exception as e:
            return None, None
        return width, height

    def compare_image_similarity(self, image1, image2):
        # Load images
        image1 = cv2.imread(image1)
        image2 = cv2.imread(image2)
        image2 = cv2.resize(
            image2, (image1.shape[1], image1.shape[0]), interpolation=cv2.INTER_AREA
        )
        # Convert images to grayscale
        image1_gray = cv2.cvtColor(image1, cv2.COLOR_BGR2GRAY)
        image2_gray = cv2.cvtColor(image2, cv2.COLOR_BGR2GRAY)
        # Calculate SSIM
        ssim_score = metrics.structural_similarity(image1_gray, image2_gray, full=True)
        return ssim_score[0]
