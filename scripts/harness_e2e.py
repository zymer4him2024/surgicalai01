import requests
import time
import sys

BASE_URL = "http://localhost:8005"  # Device Master Agent
MOCK_MDM_URL = "http://localhost:8017" # Mock MDM (External port)

def test_health():
    print("Checking health of services...")
    try:
        resp = requests.get(f"{BASE_URL}/health")
        resp.raise_for_status()
        print(f"Device Master Agent: OK - {resp.json()['status']}")
        
        resp = requests.get(f"{MOCK_MDM_URL}/health")
        resp.raise_for_status()
        print(f"Mock Customer MDM: OK - {resp.json()['status']}")
    except Exception as e:
        print(f"Health check failed: {e}")
        # sys.exit(1)

def test_lookup(label):
    print(f"\nPerforming lookup for label: {label}")
    try:
        resp = requests.get(f"{BASE_URL}/device/lookup", params={"label": label})
        if resp.status_code == 200:
            data = resp.json()
            print(f"Success! [{data['data_source']}]")
            print(f"  Standard Name: {data['device_name']}")
            print(f"  Product Code:  {data['product_code']}")
            print(f"  Device Class:  {data['device_class']}")
            return data
        else:
            print(f"Lookup failed with status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"Lookup error: {e}")
    return None

if __name__ == "__main__":
    test_health()
    
    # 1. Test MDM hit (forceps is in mock catalog)
    test_lookup("forceps")
    
    # 2. Test MDM hit (scalpel is in mock catalog)
    test_lookup("scalpel")
    
    # 3. Test fallback (if 'dummy' is in labels.json but not in MDM)
    test_lookup("dummy")
    
    # 4. Test 404 (totally unknown label)
    test_lookup("unknown_instrument_123")
