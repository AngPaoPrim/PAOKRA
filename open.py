import json
import os
from playwright.sync_api import sync_playwright

COOKIE_FILE = "shop.tiktok.com_cookies.txt"
SOURCE_URL = "https://shop.tiktok.com/streamer/live/product/dashboard"
TARGET_URL = "https://business.tiktokshop.com/us/creator/live"

def load_and_convert_cookies(filepath):
    """Load Netscape cookies locally without printing cookie values."""
    if not os.path.exists(filepath):
        print(f"ไม่พบไฟล์คุกกี้: {filepath}")
        return []

    playwright_cookies = []
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split("\\t")
            if len(parts) >= 7:
                domain, flag, path, secure, expiration, name, value = parts[:7]
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path or "/",
                    "secure": secure.lower() == "true",
                    "httpOnly": False,
                }
                if expiration.isdigit() and int(expiration) > 0:
                    cookie["expires"] = int(expiration)
                playwright_cookies.append(cookie)
            else:
                try:
                    data = json.loads(line)
                    if isinstance(data, list):
                        return data
                except Exception:
                    pass
    return playwright_cookies

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(no_viewport=True)

        cookies = load_and_convert_cookies(COOKIE_FILE)
        if cookies:
            context.add_cookies(cookies)
            print(f"โหลดคุกกี้สำเร็จ {len(cookies)} รายการ")
        else:
            print("คำเตือน: ไม่มีคุกกี้ที่ใช้ได้")

        page = context.new_page()
        print(f"กำลังเปิดหน้า source: {SOURCE_URL}")
        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # ใช้ browser context เดิมต่อไป เพื่อให้ cookies/storage/session
        # ที่หน้า TikTok ตั้งขึ้นระหว่างการโหลด ถูกคงไว้ตอนเปลี่ยนไปยังปลายทาง
        print(f"กำลังไปหน้า creator: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        print("\\nเบราว์เซอร์เปิดอยู่ ใช้งานต่อได้เลย")
        print("กด Enter ใน Terminal เมื่อต้องการปิดเบราว์เซอร์")
        input()
        browser.close()

if __name__ == "__main__":
    main()
