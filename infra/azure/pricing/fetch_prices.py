"""Download Azure Retail Prices for South Africa North, one file per service (USD, consumption prices)."""
import json, sys, time, urllib.parse, urllib.request
OUT = sys.argv[1]
SERVICES = ["Azure Container Apps", "Azure Database for PostgreSQL", "Redis Cache", "Storage", "Container Registry",
            "Azure Front Door Service", "Key Vault", "Log Analytics", "Virtual Machines", "Backup", "Virtual Network",
            "Bandwidth", "Microsoft Defender for Cloud", "Azure Monitor", "Application Insights", "Azure Managed Redis"]
summary = {}
for svc in SERVICES:
    flt = f"armRegionName eq 'southafricanorth' and serviceName eq '{svc}' and priceType eq 'Consumption'"
    url = "https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=" + urllib.parse.quote(flt)
    items, page = [], url
    while page:
        with urllib.request.urlopen(page, timeout=60) as r:
            data = json.load(r)
        items += data.get("Items", [])
        page = data.get("NextPageLink")
        time.sleep(0.2)
    json.dump({"query": url, "items": items}, open(f"{OUT}/{svc.replace(' ', '_')}.json", "w"), indent=0)
    summary[svc] = len(items)
print(json.dumps(summary, indent=1))
