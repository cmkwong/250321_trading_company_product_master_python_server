from PIL import Image
import io
import win32clipboard  # For Windows clipboard operations

def copy_image_to_clipboard(image_path):
    """Helper function to copy an image to clipboard"""
    image = Image.open(image_path)

    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]  # Remove BMP header
    output.close()

    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    win32clipboard.CloseClipboard()