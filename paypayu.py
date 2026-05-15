import requests
import datetime
import os
# useragent-changerライブラリが必要です: pip install useragent-changer
try:
    from useragent_changer import UserAgent
    ua = UserAgent('iphone')
except ImportError:
    # ライブラリがない場合のフォールバック
    class MockUA:
        def set(self):
            return "PayPay/3.45.0 (iPhone; iOS 15.0; Scale/3.00)"
    ua = MockUA()

PROXY_URL = os.getenv('PROXY_URL')
PROXIES = {
    'http': PROXY_URL,
    'https': PROXY_URL
} if PROXY_URL else None

# --- send login request ---
def login(phoneNumber: str, password: str, uuid: str):
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": f"{uuid}",
        "grant_type": "password",
        "username": phoneNumber,
        "password": password,
        "add_otp_prefix": True,
        "language": "ja"
    }
    try:
        response = requests.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=headers, json=payload, proxies=PROXIES)
        return response.json()
    except Exception as e:
        return {"response_type": "ErrorResponse", "message": str(e)}

# --- one-time-password authentication ---
def login_otp(set_uuid, otp, otpid, otp_pre):
    otp_number = otp
    headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://www.paypay.ne.jp/app/account/sign-in',
    }
    payload = {
        "scope": "SIGN_IN",
        "client_uuid": f"{set_uuid}",
        "grant_type": "otp",
        "otp_prefix": str(otp_pre),
        "otp": otp_number,
        "otp_reference_id": otpid,
        "username_type": "MOBILE",
        "language": "ja"
    }
    try:
        response = requests.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=headers, json=payload, proxies=PROXIES)
        login_response = response.json()
        if login_response.get("response_type") == "ErrorResponse":
            return "ERR"
        return "OK"
    except:
        return "ERR"

def check_link(cd):
    if "https://" in cd:
        cd = cd.replace("https://pay.paypay.ne.jp/", "")

    headers = {
        "Accept": "application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=headers, proxies=PROXIES)
        response.raise_for_status()
        link_info = response.json()
    except requests.exceptions.RequestException as e:
        print(f"API_REQ_EXC: {e}")
        return False

    result_code = link_info.get("header", {}).get("resultCode")
    if result_code != "S0000":
        return False

    order_status = link_info.get("payload", {}).get("orderStatus")
    if order_status == "PENDING":
        return link_info
    else:
        return False
    
def link_rev(cd: str, phoneNumber: str, password: str, uuid: str, link_password: str = None):
    if "https://" in cd:
        cd = cd.replace("https://pay.paypay.ne.jp/", "")

    session = requests.Session()

    base_headers = {
        "Accept": "application/json, text/plain, */*",
        'User-Agent': ua.set(),
        "Content-Type": "application/json"
    }

    # 1. リンク情報の取得
    try:
        response = session.get(f"https://www.paypay.ne.jp/app/v2/p2p-api/getP2PLinkInfo?verificationCode={cd}", headers=base_headers, proxies=PROXIES)
        response.raise_for_status()
        link_info = response.json()

        if link_info.get("payload", {}).get("orderStatus") != "PENDING":
            return False

        if link_info.get("payload", {}).get("pendingP2PInfo", {}).get("isSetPasscode") and link_password is None:
            return False

    except requests.exceptions.RequestException as e:
        print(f"LINK_REQ_EXC: {e}")
        return False

    # 2. ログインしてトークン取得
    login_payload = {
        "scope": "SIGN_IN",
        "client_uuid": f"{uuid}",
        "grant_type": "password",
        "username": phoneNumber,
        "password": password,
        "add_otp_prefix": True,
        "language": "ja"
    }

    login_headers = {
        'User-Agent': ua.set(),
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://www.paypay.ne.jp',
        'Referer': 'https://pay.paypay.ne.jp/' + cd,
    }

    try:
        response = session.post("https://www.paypay.ne.jp/app/v1/oauth/token", headers=login_headers, json=login_payload, proxies=PROXIES)
        login_response = response.json()
        
        access_token = login_response.get("access_token")
        if not access_token:
            return "LOGINERR"
            
    except Exception as e:
        print(f"LOGIN_EXC: {e}")
        return "LOGINERR"

    # ★修正: 取得したトークンをヘッダーにセット
    base_headers["Authorization"] = f"Bearer {access_token}"

    # 3. 受け取りリクエスト
    receive_payload = {
        "verificationCode": cd,
        "client_uuid": uuid,
        "requestAt": str(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900')),
        "requestId": link_info["payload"]["message"]["data"]["requestId"],
        "orderId": link_info["payload"]["message"]["data"]["orderId"],
        "senderMessageId": link_info["payload"]["message"]["messageId"],
        "senderChannelUrl": link_info["payload"]["message"]["chatRoomId"],
        "iosMinimumVersion": "3.45.0",
        "androidMinimumVersion": "3.45.0"
    }

    if link_password:
        receive_payload["passcode"] = link_password

    try:
        response = session.post("https://www.paypay.ne.jp/app/v2/p2p-api/acceptP2PSendMoneyLink", json=receive_payload, headers=base_headers, proxies=PROXIES)
        response.raise_for_status()
        receive_data = response.json()

        if receive_data.get("header", {}).get("resultCode") == "S0000":
            return True
        else:
            print(f"RECEIVE_FAIL: {receive_data}") # debug
            return False

    except requests.exceptions.RequestException as e:
        print(f"REVERR: {e}")
        return False