import os
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver import  ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from fake_useragent import UserAgent

from bs4 import BeautifulSoup

import pyperclip
import matplotlib.pyplot as plt
import requests
from urllib.parse import urlparse
from PIL import Image

from codes import config
from codes.utils import fileModel
from codes.controllers.PyautoController import PyautoController

class WebController:
    def __init__(self):
        executable_path = os.path.join(config.CHROME_WEB_DRIVER_PATH, 'chromedriver.exe')

        self.service = Service(executable_path=executable_path)
        self.options = webdriver.ChromeOptions()
        self.driver = None
        self.is_quit = True

        # controller
        self.PATTERN_1688_PATH = os.path.join(config.AUTO_IMAGE, '1688')
        self.pyautoController = PyautoController()

    def get_image_size(self, image_path):
        img = plt.imread(image_path)
        height, width = img.shape[:2]
        return width, height

    def download_image(self, url, save_path=None):
        temp_path = None
        try:
            # Send GET request
            response = requests.get(url, stream=True)
            response.raise_for_status()

            # Determine filename and path
            if not save_path:
                filename = url.split('/')[-1]
                save_path = os.path.join(os.getcwd(), filename)
            else:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)

            # Create temp path for initial download
            base, ext = os.path.splitext(save_path)
            temp_path = f"{base}_temp{ext}"

            # Save the image to temp location
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)

            # Get dimensions - using separate with block to ensure file is closed
            with Image.open(temp_path) as img:
                width, height = img.size

            # Close the image explicitly (extra precaution)
            img.close()

            # Construct new filename with dimensions
            new_path = f"{base}_{width}x{height}{ext}"

            # Rename temp file to final path
            os.replace(temp_path, new_path)

            print(f"Image successfully saved to {new_path}")
            return new_path

        except Exception as e:
            print(f"Error downloading image: {e}")
            # Clean up temp file if it exists
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
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
            # making fake agent
            # ua = UserAgent()
            # user_agent = ua.random
            # self.options.add_argument(f'user-agent={user_agent}')
            # create driver
            self.driver = webdriver.Chrome(service=self.service, options=self.options)
            self.is_quit = False
        self.driver.get(website)

    def download_1688_images_from_html(self, product_id):
        # finding first image
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'reference_1688.png')], 50, 500)
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'first_video.png')], 20, 0, click=False)
        # self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'reference_1688.png')], 50, 500, grayscale=False)
        for _ in range(4):
            self.pyautoController.scroll_startend(False)
            time.sleep(0.5)
            self.pyautoController.scroll_startend(True)
            time.sleep(0.5)
        # getting html text
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'reference_1688.png')], 50, 500)
        self.pyautoController.press_key('F12')
        time.sleep(1)
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'html_text.png')], left=False)
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'copy.png')])
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'copy_element.png')])
        self.pyautoController.findPattern_and_click([os.path.join(self.PATTERN_1688_PATH, 'reference_1688.png')], 50, 500)
        self.pyautoController.press_key('F12')
        # getting html text
        website_html_txt = pyperclip.paste()
        # website_html_txt = fileModel.read_text(config.DOCS, "html.txt")
        soup = BeautifulSoup(website_html_txt)
        # set variable
        counts = {'display': 1, 'video': 1, 'description': 1}

        # find videos
        video_urls = []
        videos = soup.find_all('video', {"class": 'lib-video'})
        for video in videos:
            url = video['src']
            video_urls.append(url)
            self._download_naming_src(url, product_id, counts['video'], 'video')
            counts['video'] += 1

        # find display images
        display_images = soup.find_all('div', {"class": 'detail-gallery-turn-wrapper'})
        display_image_urls = []
        for display_image in display_images:
            url = display_image.find('img')['src']
            display_image_urls.append(url)
            self._download_naming_src(url, product_id, counts['display'], 'display')
            counts['display'] += 1

        # find description image
        description_image_urls = []
        description_images = soup.find_all('img', {"class": 'desc-img-loaded'})
        for description_image in description_images:
            url = description_image['src']
            description_image_urls.append(url)
            self._download_naming_src(url, product_id, counts['description'], 'description')
            counts['description'] += 1
        return True

    def _download_naming_src(self, url, product_id, count, src_type='video'):
        ext = fileModel.getFileExt(url)
        count_str = f"{count}".zfill(2)
        filename = f"{src_type}_{product_id}_{count_str}{ext}"
        if src_type == 'video':
            self.download_video(url, os.path.join(config.PRODUCT_FOLDER_PATH, product_id, src_type, filename))
        else:
            self.download_image(url, os.path.join(config.PRODUCT_FOLDER_PATH, product_id, src_type, filename))

    def download_1688_images(self, website, product_id, quit_finally=False):
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
                    self._download_naming_src(video_url, product_id, counts['video'], 'video')
                    counts['video'] += 1
                else:
                    image_url = is_image[0].get_attribute('src')
                    self._download_naming_src(image_url, product_id, counts['display'], 'display')
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
                self._download_naming_src(des_img_url, product_id, counts['description'], 'description')
                counts['description'] += 1
        finally:
            if quit_finally:
                self.driver.quit()
                self.is_quit = True
        return


# webController = WebController()
# webController.download_1688_images(r"https://detail.1688.com/offer/694280859637.html?spm=a26352.13672862.offerlist.165.3b9d1e62Z7KTLn&cosite=-&tracelog=p4p&_p_isad=1&clickid=7e80672c34f44c508bee9cff5492974a&sessionid"
#                                    r"=9fd256fb821841c88875fdec1bc6ac96", '202504181233')
