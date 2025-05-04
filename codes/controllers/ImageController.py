import io
import os

import torch
import requests
import base64
from pathlib import Path
from codes import config
from codes.utils import fileModel
from codes.controllers.AIToolsController import AIToolsController

class ImageController:
    def __init__(self):
        # self.API_URL = "https://api.deepseek.com/v1/ocr"
        self.API_URL = r"https://www.imagetotext.info/api/imageToText"
        self.aiToolsController = AIToolsController()

    def image_to_base64(self, image_folder, image_name):
        """
        Helper method to convert an image file to base64 encoded string with data URI

        Args:
            image_path (str): Path to the image file

        Returns:
            str: Base64 encoded image with data URI prefix
        """
        image_path = os.path.join(image_folder, image_name)
        with open(image_path, 'rb') as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            extension = Path(image_path).suffix[1:]  # Get file extension without dot
            return f"data:image/{extension};base64,{encoded_string}"

    def extract_text_with_image2text(self, image_folder, image_name):

        # Encode image in base64
        # encoded_image = base64.b64encode(image_data).decode('utf-8')
        encoded_image = self.image_to_base64(image_folder, image_name)

        # Prepare headers and payload
        headers = {
            "Authorization": f"Bearer {config.IMAGETOTEXT_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "base64": encoded_image,
            # "image_url": os.path.join(image_folder, image_name)
            # "language": "zh",  # Chinese for your example image
            # "detail": True  # Set True if you need position data
        }

        try:
            # Make API request
            response = requests.post(self.API_URL, headers=headers, json=payload)
            response.raise_for_status()

            # Parse response
            result = response.json()
            txt = result.get('result', 'No text found')
            txt = txt.replace('<br />', '').replace('\n\n', '\n')
            return txt

        except requests.exceptions.RequestException as e:
            return f"API Error: {str(e)}"
        except Exception as e:
            return f"Error: {str(e)}"

    def write_extracted_txt(self, product_index, sub_prj_folder='display'):
        folder = os.path.join(config.PRODUCT_FOLDER_PATH, product_index, sub_prj_folder)
        images = fileModel.getFileList(folder)
        txt = ''
        for img in images:
            extracted_txt = self.extract_text_with_image2text(folder, img)
            extracted_txt = f"---- {img} ----\n{extracted_txt}\n"
            txt += extracted_txt
        fileModel.write_txt(folder, f'{sub_prj_folder}_{product_index}.txt', txt, 'w')
        translated_txt = self.aiToolsController.translate_content_simple(txt, 'Chinese', "English")
        fileModel.write_txt(folder, f'{sub_prj_folder}_{product_index}_translated.txt', f"{translated_txt}\n", 'w')