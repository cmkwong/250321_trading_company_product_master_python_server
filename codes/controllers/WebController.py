import os
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver import  ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import wget
import requests
from urllib.parse import urlparse

from codes import config
from codes.utils import fileModel

class WebController:
    def __init__(self):
        executable_path = os.path.join(config.CHROME_WEB_DRIVER_PATH, 'chromedriver.exe')
        self.service = Service(executable_path=executable_path)
        self.options = webdriver.ChromeOptions()
        self.driver = None
        self.is_quit = True

    def download_image(self, url, save_path=None):
        try:
            # Send GET request
            response = requests.get(url, stream=True)
            response.raise_for_status()  # Raise error for bad status codes

            # Determine filename and path
            if not save_path:
                filename = url.split('/')[-1]  # Extract filename from URL
                save_path = os.path.join(os.getcwd(), filename)
            else:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)

            # Save the image
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)

            print(f"Image successfully saved to {save_path}")
            return save_path

        except Exception as e:
            print(f"Error downloading image: {e}")
            return None

    def download_video(self, url, save_path=None):
        try:
            # Set headers to mimic a browser request
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                # 'Referer': 'https://taobao.com/'
            }

            # Start the download session
            with requests.get(url, headers=headers, stream=True) as r:
                r.raise_for_status()  # Raise error for bad status codes

                # Determine filename
                if save_path:
                    filename = save_path
                else:
                    # Extract filename from URL if not provided
                    filename = os.path.basename(urlparse(url).path)
                    if not filename:
                        # Fallback filename if URL doesn't contain one
                        filename = "video.mp4"

                # Create directory if it doesn't exist
                os.makedirs(os.path.dirname(filename), exist_ok=True)

                # Download with progress
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0

                with open(filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:  # filter out keep-alive chunks
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = downloaded * 100 / total_size
                                print(f"Downloaded: {downloaded}/{total_size} bytes ({percent:.2f}%)", end='\r')

                print(f"\nVideo successfully downloaded to: {os.path.abspath(filename)}")
                return filename

        except Exception as e:
            print(f"Error downloading video: {e}")
            return None

    def init_driver(self):
        if self.driver:
            self.driver.quit()
            self.driver = webdriver.Chrome(service=self.service, options=self.options)
            self.driver.get("www.google.com")

    def open_driver(self, website):
        if self.is_quit:
            self.driver = webdriver.Chrome(service=self.service, options=self.options)
            self.is_quit = False
        self.driver.get(website)

    def download_1688_images(self, website, product_id):
        # setup counter
        counts = {'display': 1, 'video': 1, 'description': 1}
        try:
            self.open_driver(website)
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".pi-layout-container")))
            time.sleep(1)

            # setup action chains
            actions = ActionChains(self.driver)
            # find the display element
            elements = self.driver.find_elements(By.CLASS_NAME, r'detail-gallery-turn-wrapper')
            for element in elements:
                actions.move_to_element(element).perform()
                videos = element.find_elements(By.CLASS_NAME, 'video-icon')
                is_image = element.find_elements(By.CLASS_NAME, 'detail-gallery-img')
                if len(videos) > 0:
                    # getting extension
                    video_url = self.driver.find_element(By.CSS_SELECTOR, '.lib-video.vjs-has-started').find_element(By.TAG_NAME, 'video').get_attribute('src')
                    ext = fileModel.getFileExt(video_url)
                    filename = f"video_{product_id}_{counts['video']}{ext}"
                    self.download_video(video_url, os.path.join(config.PRODUCT_FOLDER_PATH, product_id, 'video', filename))
                    counts['video'] += 1
                else:
                    image_url = is_image[0].get_attribute('src')
                    ext = fileModel.getFileExt(image_url)
                    filename = f"img_{product_id}_{counts['display']}{ext}"
                    self.download_image(image_url, os.path.join(config.PRODUCT_FOLDER_PATH, product_id, 'display', filename))
                    counts['display'] += 1
                time.sleep(0.5)
            # find the product description
            # Scroll to bottom
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # Wait for new content to load
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2)
            elements = self.driver.find_elements(By.CLASS_NAME, 'desc-img-loaded')
            for i, element in enumerate(elements):
                des_img_url = element.get_attribute('src')
                ext = fileModel.getFileExt(des_img_url)
                filename = f"des_{product_id}_{i+1}{ext}"
                self.download_image(des_img_url,  os.path.join(config.PRODUCT_FOLDER_PATH, product_id, 'description', filename))
        finally:
            self.driver.quit()
            self.is_quit = True
        return


# webController = WebController()
# webController.download_1688_images(r"https://detail.1688.com/offer/694280859637.html?spm=a26352.13672862.offerlist.165.3b9d1e62Z7KTLn&cosite=-&tracelog=p4p&_p_isad=1&clickid=7e80672c34f44c508bee9cff5492974a&sessionid"
#                                    r"=9fd256fb821841c88875fdec1bc6ac96", '202504181233')
