#!/usr/bin/env python3
"""
Test script for all 5 vertical app endpoints on the server.
Usage: python test_vertical_endpoints.py <server_url> <access_token>

Example:
    python test_vertical_endpoints.py https://api.yourdomain.com your_oauth_token_here
"""

import sys
import requests
import json
from datetime import datetime

# Color codes for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'


def print_success(message):
    print(f"{GREEN}OK {message}{RESET}")


def print_error(message):
    print(f"{RED}FAIL {message}{RESET}")


def print_info(message):
    print(f"{BLUE}ℹ {message}{RESET}")


def print_warning(message):
    print(f"{YELLOW}{message}{RESET}")


def test_endpoint(base_url, endpoint, token, method='GET', data=None, description=""):
    """Test a single endpoint"""
    url = f"{base_url}{endpoint}"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    print_info(f"Testing: {description or endpoint}")
    print(f"  URL: {url}")
    print(f"  Method: {method}")
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=30)
        elif method == 'POST':
            response = requests.post(url, headers=headers, json=data, timeout=30)
        elif method == 'PUT':
            response = requests.put(url, headers=headers, json=data, timeout=30)
        elif method == 'DELETE':
            response = requests.delete(url, headers=headers, timeout=30)
        
        if response.status_code in [200, 201, 202]:
            print_success(f"Status: {response.status_code}")
            print(f"  Response: {json.dumps(response.json(), indent=2)[:200]}...")
            return True, response.json()
        else:
            print_error(f"Status: {response.status_code}")
            print(f"  Error: {response.text[:200]}")
            return False, None
            
    except Exception as e:
        print_error(f"Exception: {str(e)}")
        return False, None


def main():
    if len(sys.argv) < 3:
        print_error("Usage: python test_vertical_endpoints.py <server_url> <access_token>")
        print_info("Example: python test_vertical_endpoints.py https://api.yourdomain.com your_token")
        sys.exit(1)
    
    base_url = sys.argv[1].rstrip('/')
    token = sys.argv[2]
    
    print_info(f"Testing vertical endpoints on: {base_url}")
    print_info(f"Using token: {token[:20]}...")
    print("\n" + "="*80 + "\n")
    
    # Test results tracking
    results = {
        'total': 0,
        'passed': 0,
        'failed': 0
    }
    
    # ========================================================================
    # 1. PRIVATE EQUITY ENDPOINTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("1. PRIVATE EQUITY - Due Diligence")
    print(f"{'='*80}{RESET}\n")
    
    endpoints = [
        ('GET', '/api/v1/private-equity/due-diligence-runs/', None, 'List Due Diligence Runs'),
        ('GET', '/api/v1/private-equity/risk-clause-summary/', None, 'Risk Clause Summary'),
        ('GET', '/api/v1/private-equity/document-type-summary/', None, 'Document Type Summary'),
        ('GET', '/api/v1/private-equity/service-executions/', None, 'Service Executions'),
    ]
    
    for method, endpoint, data, desc in endpoints:
        results['total'] += 1
        success, _ = test_endpoint(base_url, endpoint, token, method, data, desc)
        if success:
            results['passed'] += 1
        else:
            results['failed'] += 1
        print()
    
    # ========================================================================
    # 2. CLASS ACTIONS ENDPOINTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("2. CLASS ACTIONS - Mass Claims")
    print(f"{'='*80}{RESET}\n")
    
    endpoints = [
        ('GET', '/api/v1/class-actions/mass-claims-runs/', None, 'List Mass Claims Runs'),
        ('GET', '/api/v1/class-actions/intake-forms/', None, 'List Intake Forms'),
        ('GET', '/api/v1/class-actions/evidence-documents/', None, 'List Evidence Documents'),
        ('GET', '/api/v1/class-actions/service-executions/', None, 'Service Executions'),
    ]

    for method, endpoint, data, desc in endpoints:
        results['total'] += 1
        success, _ = test_endpoint(base_url, endpoint, token, method, data, desc)
        if success:
            results['passed'] += 1
        else:
            results['failed'] += 1
        print()

    # ========================================================================
    # 3. LABOR & EMPLOYMENT ENDPOINTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("3. LABOR & EMPLOYMENT - Workplace Communications")
    print(f"{'='*80}{RESET}\n")

    endpoints = [
        ('GET', '/api/v1/labor-employment/communications-runs/', None, 'List Communications Runs'),
        ('GET', '/api/v1/labor-employment/message-analysis-summary/', None, 'Message Analysis Summary'),
        ('GET', '/api/v1/labor-employment/compliance-alert-summary/', None, 'Compliance Alert Summary'),
        ('GET', '/api/v1/labor-employment/wage-hour-summary/', None, 'Wage Hour Summary'),
        ('GET', '/api/v1/labor-employment/service-executions/', None, 'Service Executions'),
    ]

    for method, endpoint, data, desc in endpoints:
        results['total'] += 1
        success, _ = test_endpoint(base_url, endpoint, token, method, data, desc)
        if success:
            results['passed'] += 1
        else:
            results['failed'] += 1
        print()

    # ========================================================================
    # 4. IP LITIGATION ENDPOINTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("4. IP LITIGATION - Patent Analysis")
    print(f"{'='*80}{RESET}\n")

    endpoints = [
        ('GET', '/api/v1/ip-litigation/analysis-runs/', None, 'List Patent Analysis Runs'),
        ('GET', '/api/v1/ip-litigation/patent-documents/', None, 'List Patent Documents'),
        ('GET', '/api/v1/ip-litigation/patent-claims/', None, 'List Patent Claims'),
        ('GET', '/api/v1/ip-litigation/claim-charts/', None, 'List Claim Charts'),
        ('GET', '/api/v1/ip-litigation/patent-analysis-summary/', None, 'Patent Analysis Summary'),
        ('GET', '/api/v1/ip-litigation/service-executions/', None, 'Service Executions'),
    ]

    for method, endpoint, data, desc in endpoints:
        results['total'] += 1
        success, _ = test_endpoint(base_url, endpoint, token, method, data, desc)
        if success:
            results['passed'] += 1
        else:
            results['failed'] += 1
        print()

    # ========================================================================
    # 5. REGULATORY COMPLIANCE ENDPOINTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("5. REGULATORY COMPLIANCE - GDPR/CCPA")
    print(f"{'='*80}{RESET}\n")

    endpoints = [
        ('GET', '/api/v1/regulatory-compliance/compliance-runs/', None, 'List Compliance Runs'),
        ('GET', '/api/v1/regulatory-compliance/regulatory-requirements/', None, 'List Regulatory Requirements'),
        ('GET', '/api/v1/regulatory-compliance/dsar-requests/', None, 'List DSAR Requests'),
        ('GET', '/api/v1/regulatory-compliance/compliance-summary/', None, 'Compliance Summary'),
        ('GET', '/api/v1/regulatory-compliance/service-executions/', None, 'Service Executions'),
    ]

    for method, endpoint, data, desc in endpoints:
        results['total'] += 1
        success, _ = test_endpoint(base_url, endpoint, token, method, data, desc)
        if success:
            results['passed'] += 1
        else:
            results['failed'] += 1
        print()

    # ========================================================================
    # FINAL RESULTS
    # ========================================================================
    print(f"\n{BLUE}{'='*80}")
    print("TEST RESULTS SUMMARY")
    print(f"{'='*80}{RESET}\n")

    print(f"Total Tests: {results['total']}")
    print_success(f"Passed: {results['passed']}")
    print_error(f"Failed: {results['failed']}")

    success_rate = (results['passed'] / results['total'] * 100) if results['total'] > 0 else 0
    print(f"\nSuccess Rate: {success_rate:.1f}%")

    if results['failed'] == 0:
        print_success("\nAll tests passed!")
    else:
        print_warning(f"\n{results['failed']} test(s) failed. Check the output above for details.")


if __name__ == '__main__':
    main()


