"""
SharePoint Cookie Auto-Extractor (v2)
======================================
Extracts FedAuth/rtFa cookies from Edge/Chrome using direct DPAPI decryption.
Works even when the browser is running (copies the cookie DB first).
"""

import os
import sys
import json
import shutil
import sqlite3
import tempfile
import base64

from sp_sync.db.store import get_store

SP_DOMAIN = "sharepoint.com"


def _get_encryption_key(browser_local_state_path):
    """Get the AES encryption key from the browser's Local State file."""
    try:
        with open(browser_local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        
        encrypted_key = base64.b64decode(local_state['os_crypt']['encrypted_key'])
        # Remove 'DPAPI' prefix (first 5 bytes)
        encrypted_key = encrypted_key[5:]
        
        # Decrypt using Windows DPAPI
        import ctypes
        import ctypes.wintypes
        
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ('cbData', ctypes.wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_char))
            ]
        
        blob_in = DATA_BLOB(len(encrypted_key), ctypes.create_string_buffer(encrypted_key, len(encrypted_key)))
        blob_out = DATA_BLOB()
        
        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return key
        return None
    except Exception as e:
        return None


def _decrypt_cookie_value(encrypted_value, key):
    """Decrypt a cookie value using AES-GCM with the provided key."""
    try:
        if encrypted_value[:3] == b'v10' or encrypted_value[:3] == b'v20':
            # AES-256-GCM
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            
            from Cryptodome.Cipher import AES
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
        else:
            # Old DPAPI encryption
            import ctypes
            import ctypes.wintypes
            
            class DATA_BLOB(ctypes.Structure):
                _fields_ = [
                    ('cbData', ctypes.wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))
                ]
            
            blob_in = DATA_BLOB(len(encrypted_value), ctypes.create_string_buffer(encrypted_value, len(encrypted_value)))
            blob_out = DATA_BLOB()
            
            if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                value = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode('utf-8')
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return value
        return None
    except Exception:
        return None


def _extract_from_browser(browser_name, user_data_dir, local_state_path):
    """Extract SharePoint cookies from a Chromium-based browser."""
    results = {'fedauth': '', 'rtfa': '', 'found_cookies': 0}
    
    # Find cookie database
    cookie_paths = []
    if browser_name == 'Edge':
        cookie_paths = [
            os.path.join(user_data_dir, 'Default', 'Network', 'Cookies'),
            os.path.join(user_data_dir, 'Default', 'Cookies'),
        ]
    else:  # Chrome
        cookie_paths = [
            os.path.join(user_data_dir, 'Default', 'Network', 'Cookies'),
            os.path.join(user_data_dir, 'Default', 'Cookies'),
        ]
    
    cookie_db = None
    for p in cookie_paths:
        if os.path.exists(p):
            cookie_db = p
            break
    
    if not cookie_db:
        return results, f"No Cookies database found for {browser_name}"
    
    # Get encryption key
    key = _get_encryption_key(local_state_path)
    if not key:
        return results, f"Could not read encryption key for {browser_name}"
    
    # Copy cookie DB to temp (to avoid locking issues when browser is running)
    tmp_db = os.path.join(tempfile.gettempdir(), f'sp_cookies_{browser_name.lower()}.db')
    copy_success = False
    
    # Method 1: Direct copy (works if browser is closed)
    try:
        shutil.copy2(cookie_db, tmp_db)
        copy_success = True
    except (PermissionError, OSError):
        pass
    
    # Method 2: Use shadowcopy package (Windows VSS)
    if not copy_success:
        try:
            import shadowcopy
            shadowcopy.copy(cookie_db, tmp_db)
            copy_success = True
        except Exception:
            pass
    
    # Method 3: PowerShell-based raw copy (bypasses some locks)
    if not copy_success:
        try:
            import subprocess
            # Use .NET File class to read locked files
            ps_cmd = (
                f'$bytes = [System.IO.File]::ReadAllBytes("{cookie_db}"); '
                f'[System.IO.File]::WriteAllBytes("{tmp_db}", $bytes)'
            )
            result_ps = subprocess.run(
                ['powershell', '-Command', ps_cmd],
                capture_output=True, timeout=10
            )
            if result_ps.returncode == 0 and os.path.exists(tmp_db):
                copy_success = True
        except Exception:
            pass
    
    # Method 4: Raw binary read with retry
    if not copy_success:
        try:
            with open(cookie_db, 'rb') as src:
                data = src.read()
            with open(tmp_db, 'wb') as dst:
                dst.write(data)
            copy_success = True
        except Exception:
            pass
    
    if not copy_success:
        return results, f"Could not copy {browser_name} cookie database. Close the browser and try again."
    
    try:
        conn = sqlite3.connect(tmp_db)
        cursor = conn.cursor()
        
        # Query for SharePoint cookies
        cursor.execute(
            "SELECT name, encrypted_value, host_key FROM cookies WHERE host_key LIKE ?",
            (f'%{SP_DOMAIN}%',)
        )
        
        for name, encrypted_value, host in cursor.fetchall():
            results['found_cookies'] += 1
            if name in ('FedAuth', 'rtFa') and encrypted_value:
                decrypted = _decrypt_cookie_value(encrypted_value, key)
                if decrypted:
                    if name == 'FedAuth':
                        results['fedauth'] = decrypted
                    elif name == 'rtFa':
                        results['rtfa'] = decrypted
        
        conn.close()
    except Exception as e:
        return results, f"Database read error: {e}"
    finally:
        try:
            os.remove(tmp_db)
        except:
            pass
    
    return results, None


def extract_sp_cookies():
    """Extract SharePoint cookies from Edge or Chrome browser."""
    result = {'fedauth': '', 'rtfa': '', 'source': '', 'error': '', 'details': []}
    
    local_app_data = os.environ.get('LOCALAPPDATA', '')
    
    browsers = [
        (
            'Edge',
            os.path.join(local_app_data, 'Microsoft', 'Edge', 'User Data'),
            os.path.join(local_app_data, 'Microsoft', 'Edge', 'User Data', 'Local State'),
        ),
        (
            'Chrome',
            os.path.join(local_app_data, 'Google', 'Chrome', 'User Data'),
            os.path.join(local_app_data, 'Google', 'Chrome', 'User Data', 'Local State'),
        ),
    ]
    
    for browser_name, user_data_dir, local_state_path in browsers:
        if not os.path.exists(local_state_path):
            result['details'].append(f"{browser_name}: not installed or path missing")
            continue
        
        data, error = _extract_from_browser(browser_name, user_data_dir, local_state_path)
        
        if error:
            result['details'].append(f"{browser_name}: {error}")
            continue
        
        result['details'].append(f"{browser_name}: found {data['found_cookies']} cookies for {SP_DOMAIN}")
        
        if data['fedauth']:
            result['fedauth'] = data['fedauth']
            result['rtfa'] = data['rtfa']
            result['source'] = browser_name
            return result
    
    if not result['fedauth']:
        details_str = ' | '.join(result['details']) if result['details'] else ''
        result['error'] = (
            f'FedAuth not found. '
            f'Sign in to SharePoint in Edge or Chrome first. '
            f'[{details_str}]'
        )
    
    return result


if __name__ == '__main__':
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    print("=" * 50)
    print("  Browser cookie extraction (v2)")
    print("=" * 50)
    
    res = extract_sp_cookies()
    
    for detail in res.get('details', []):
        print(f"  - {detail}")
    
    if res['fedauth']:
        print(f"\n[OK] Found FedAuth from {res['source']}")
        print(f"   Value length: {len(res['fedauth'])} characters")
        if res['rtfa']:
            print("[OK] rtFa found as well")
        
        get_store().set_sharepoint_cookies(res["fedauth"], res.get("rtfa") or "")
        print("\n[SAVE] Cookies saved to local SQLite.")
    else:
        print(f"\n[ERROR] {res['error']}")
