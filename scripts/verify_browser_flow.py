from __future__ import annotations

import json
from pathlib import Path
import uuid

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    options = Options()
    options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--proxy-server=direct://")
    options.add_argument("--proxy-bypass-list=*")
    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 120)
    marker = f"UIFLOW-{uuid.uuid4().hex[:8]}"
    try:
        driver.get("http://127.0.0.1:5173")
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".new-task")))
        wait.until(lambda current: "/c/" in current.current_url)
        initial_url = driver.current_url
        driver.find_element(By.CSS_SELECTOR, ".new-task").click()
        wait.until(lambda current: current.current_url != initial_url and "/c/" in current.current_url)
        conversation_a_url = driver.current_url

        composer = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".composer textarea")))
        composer.send_keys(f"{marker} model_v1.csv 有多少行？")
        simple_assistant_count = len(driver.find_elements(By.CSS_SELECTOR, ".message.assistant"))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".send"))).click()
        wait.until(lambda current: len(current.find_elements(By.CSS_SELECTOR, ".message.assistant")) == simple_assistant_count + 1)

        driver.find_element(By.CSS_SELECTOR, ".new-task").click()
        wait.until(lambda current: current.current_url != conversation_a_url and "/c/" in current.current_url)
        conversation_b_url = driver.current_url
        driver.refresh()
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".conversation-list")))
        assert driver.current_url == conversation_b_url

        prior = wait.until(EC.element_to_be_clickable((By.XPATH, f"//div[contains(@class,'history')][.//b[contains(.,'{marker}')]]")))
        prior.click()
        wait.until(lambda current: current.current_url == conversation_a_url)
        wait.until(lambda current: len(current.find_elements(By.CSS_SELECTOR, ".message")) == 2)

        mixed = "比较 model_v1.csv 和 model_v2.csv 的 RT 表现，分析 fused-ring 误差，并检查 training_db 训练覆盖。"
        composer = driver.find_element(By.CSS_SELECTOR, ".composer textarea")
        composer.send_keys(mixed)
        previous_assistant_count = len(driver.find_elements(By.CSS_SELECTOR, ".message.assistant"))
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".send"))).click()
        wait.until(lambda current: len(current.find_elements(By.CSS_SELECTOR, ".message.assistant")) == previous_assistant_count + 1)
        wait.until(lambda current: len(current.find_elements(By.CSS_SELECTOR, ".artifact")) >= 2)
        wait.until(lambda current: current.find_elements(By.XPATH, "//div[contains(@class,'trace-block')]/label[normalize-space()='Intent']"))

        for expected in ("Intent", "Selected Skills", "Candidate Tools", "Tool Calls"):
            if not driver.find_elements(By.XPATH, f"//div[contains(@class,'trace-block')]/label[normalize-space()='{expected}']"):
                raise AssertionError(f"missing trace section: {expected}")
        for expected in ("Plan", "Evidence", "Artifacts"):
            if not driver.find_elements(By.XPATH, f"//button[contains(@class,'fold')][contains(normalize-space(),'{expected}')]"):
                raise AssertionError(f"missing trace section: {expected}")
        screenshot = ROOT / "logs" / "product-ui-validation.png"
        driver.save_screenshot(str(screenshot))
        print(json.dumps({
            "conversation_a_url": conversation_a_url,
            "conversation_b_url": conversation_b_url,
            "marker": marker,
            "restored_message_count": 2,
            "final_assistant_messages": len(driver.find_elements(By.CSS_SELECTOR, ".message.assistant")),
            "artifact_links": [item.text for item in driver.find_elements(By.CSS_SELECTOR, ".artifact")],
            "trace_sections": ["Intent", "Selected Skills", "Candidate Tools", "Plan", "Tool Calls", "Evidence", "Artifacts"],
            "screenshot": str(screenshot),
        }, ensure_ascii=False, indent=2))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
