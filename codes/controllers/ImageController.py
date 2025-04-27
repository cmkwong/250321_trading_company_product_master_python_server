import io
import os
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = r'C:\Users\Chris\Downloads\client_secret_128094836202-05e0ot4g06e039cb5n3qfj999tvtefmk.apps.googleusercontent.com.json'

import torch
import requests
import base64

from google.cloud import vision

from codes import config
class ImageController:
    def __init__(self):
        self.API_URL = "https://api.deepseek.com/v1/ocr"

    def extract_text_with_deepseek(self, image_folder, image_name):
        with open(os.path.join(image_folder, image_name), 'rb') as image_file:
            image_data = image_file.read()

        # Encode image in base64
        encoded_image = base64.b64encode(image_data).decode('utf-8')

        # Prepare headers and payload
        headers = {
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "image": encoded_image,
            "language": "zh",  # Chinese for your example image
            "detail": True  # Set True if you need position data
        }

        try:
            # Make API request
            response = requests.post(self.API_URL, headers=headers, json=payload)
            response.raise_for_status()

            # Parse response
            result = response.json()
            return result.get('text', 'No text found')

        except requests.exceptions.RequestException as e:
            return f"API Error: {str(e)}"
        except Exception as e:
            return f"Error: {str(e)}"

    def extract_text_from_image(self, folderName, fileName):
        """
        Extracts text from an image using Google Cloud Vision API.

        Args:
            image_path (str): Path to the image file or URL

        Returns:
            str: Extracted text from the image
        """
        # Initialize the client
        client = vision.ImageAnnotatorClient()
        image_path = os.path.join(folderName, fileName)

        # Check if image_path is a URL or local file
        # if image_path.startswith(('http:', 'https:')):
        #     # Image from web
        #     image = vision.Image()
        #     image.source.image_uri = image_path
        # else:
        # Local image file
        with io.open(image_path, 'rb') as image_file:
            content = image_file.read()
        image = vision.Image(content=content)

        try:
            # Perform text detection
            response = client.text_detection(image=image)
            texts = response.text_annotations

            if texts:
                # The first annotation contains the entire detected text
                return texts[0].description
            else:
                return "No text found in the image."

        except Exception as e:
            return f"Error occurred: {str(e)}"

# imageController = ImageController()
# imageController.extract_text_from_image(r"C:\Users\Chris\Desktop\StockData\Business\Pet Product Images\202504172234\display", "img_2.jpg")
