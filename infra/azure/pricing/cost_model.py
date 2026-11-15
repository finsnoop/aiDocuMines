"""
aiDocuMines on Azure (South Africa North): monthly cost model, reproducible from Microsoft's published prices.

Every unit price is read from the Azure Retail Prices API (the source behind the Azure Pricing Calculator,
https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices). Run:

    python fetch_prices.py prices/          # downloads the price lists (one JSON per service, with the query URL)
    python cost_model.py prices/ [--zar 18]  # prints SKU, quantity, unit price, formula, USD/month and source

Quantities come from the Terraform defaults (infra/azure/terraform) and the scale test (docs/AZURE.md).
Change QUANTITIES below and re-run; nothing else is hand-typed.
"""
import argparse
import json
import pathlib

HOURS = 730            # Azure's monthly hour convention
SECONDS = HOURS * 3600

# ── Quantities (edit here) ────────────────────────────────────────────────────
Q = {
    # Container Apps, Consumption plan (per-second billing; first 180,000 vCPU-s, 360,000 GiB-s, 2M requests free)
    "consumption_apps": [
        # name, vCPU, GiB, average replicas over the month (business-hours peaks folded in, see docs/AZURE.md)
        ("api (web)", 1.0, 2.0, 3.0),
        ("frontend (nginx)", 0.25, 0.5, 2.0),
        ("backend (Node sign-in)", 0.5, 1.0, 1.0),
        ("worker-fast (Celery, threads)", 1.0, 2.0, 1.2),
        ("beat (scheduler)", 0.5, 1.0, 1.0),
        ("file-monitor", 0.5, 1.0, 1.0),
        ("litellm (AI gateway)", 1.0, 2.0, 1.0),
    ],
    "external_requests_millions": 20,       # browser requests reaching the environment per month
    # Container Apps, Dedicated plan: D4 profile (4 vCPU, 16 GiB) for the AI worker, OnlyOffice, Ollama
    "dedicated_d4_instances": 2.0,           # average instances (min 1, scales to 3 under load)
    "d4_vcpu": 4, "d4_gib": 16,
    # PostgreSQL Flexible Server, General Purpose Ddsv5, 4 vCores, 128 GiB (scale test: ~2% of a core per req/s)
    "pg_vcores": 4, "pg_storage_gb": 128, "pg_backup_extra_gb": 128, "pg_ha": False,
    # Azure Managed Redis
    "redis_sku": "B1",
    # Search VM (Elasticsearch + Milvus), Premium SSD ZRS data disk + OS disk
    "vm_sku": "Standard_D4s_v5", "data_disk": "P15 ZRS", "os_disk": "P6 LRS",
    # Azure Files (documents), Hot ZRS
    "files_gb": 500, "files_write_10k": 300, "files_read_10k": 1500, "files_list_10k": 100,
    # Front Door Premium, Zone 7 (Middle East and Africa)
    "afd_requests_10k": 2000,               # 20 M requests
    "afd_gb_to_client": 400, "afd_gb_to_origin": 100,
    # Registry, Key Vault, monitoring
    "acr_days": 30.42, "kv_ops_10k": 300,
    "log_gb": 60,                            # Log Analytics ingestion (first 5 GB/month free per billing account)
    "private_endpoints": 3, "pe_gb": 200,    # Redis, Files, Key Vault
    # Backup
    "backup_vm_instances": 1, "backup_files_instances": 1, "backup_files_gb": 500,
    # Outbound internet from Azure (mostly via Front Door, which bills its own transfer): small
    "egress_gb": 100,
    # Microsoft Defender for Cloud (opt-in in Terraform; shown for budgeting)
    "defender": False,
}


class Prices:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.cache = {}

    def find(self, file, **match):
        """Unit price for the first item whose fields equal (or, for *_contains, contain) the given values."""
        if file not in self.cache:
            self.cache[file] = json.load(open(self.folder / f"{file}.json", encoding="utf-8"))
        data = self.cache[file]
        for item in data["items"]:
            ok = True
            for k, v in match.items():
                if k.endswith("_contains"):
                    ok &= v in str(item.get(k[:-9], ""))
                else:
                    ok &= item.get(k) == v
            tier_ok = "tierMinimumUnits" in match or item.get("tierMinimumUnits", 0) in (0, 0.0)
            if ok and tier_ok:
                return item["retailPrice"], item["unitOfMeasure"], data["query"]
        raise LookupError(f"No price in {file} for {match}")


def lines(p: Prices):
    out = []

    def add(group, item, sku, qty, unit, price_unit, formula, usd, source):
        out.append(dict(group=group, item=item, sku=sku, qty=qty, unit=unit, price=price_unit, formula=formula,
                        usd=round(usd, 2), source=source))

    # Container Apps: consumption
    vcpu_s, unit_v, src = p.find("Azure_Container_Apps", meterName="Standard vCPU Active Usage")
    gib_s, unit_m, _ = p.find("Azure_Container_Apps", meterName="Standard Memory Active Usage")
    req_m, unit_r, _ = p.find("Azure_Container_Apps", meterName="Standard Requests")
    tot_vcpu = sum(c * r for _, c, _, r in Q["consumption_apps"]) * SECONDS
    tot_gib = sum(m * r for _, _, m, r in Q["consumption_apps"]) * SECONDS
    for name, c, m, r in Q["consumption_apps"]:
        add("Container Apps (Consumption)", name, f"{c} vCPU / {m} GiB", r, "avg replicas", "",
            f"{r} x ({c} vCPU x {vcpu_s} + {m} GiB x {gib_s}) x {SECONDS:,} s", r * (c * vcpu_s + m * gib_s) * SECONDS, src)
    add("Container Apps (Consumption)", "Monthly free grant", "180,000 vCPU-s + 360,000 GiB-s", 1, "subscription", "",
        f"-(180,000 x {vcpu_s} + 360,000 x {gib_s})", -min(tot_vcpu, 180000) * vcpu_s - min(tot_gib, 360000) * gib_s,
        "https://learn.microsoft.com/azure/container-apps/billing")
    add("Container Apps (Consumption)", "HTTP requests from outside the environment", "Standard Requests",
        Q["external_requests_millions"], "million", f"{req_m} / {unit_r}",
        f"max(0, {Q['external_requests_millions']} - 2 free) x {req_m}", max(0, Q["external_requests_millions"] - 2) * req_m, src)

    # Container Apps: dedicated D4
    dv, _, src = p.find("Azure_Container_Apps", meterName="Dedicated vCPU Usage")
    dm, _, _ = p.find("Azure_Container_Apps", meterName="Dedicated Memory Usage")
    mgmt, _, _ = p.find("Azure_Container_Apps", meterName="Dedicated Plan Management")
    n = Q["dedicated_d4_instances"]
    add("Container Apps (Dedicated D4)", "D4 instances (AI worker, OnlyOffice, Ollama)", "D4: 4 vCPU / 16 GiB", n,
        "avg instances", f"{dv} vCPU-h, {dm} GiB-h",
        f"{n} x ({Q['d4_vcpu']} x {dv} + {Q['d4_gib']} x {dm}) x {HOURS} h", n * (Q["d4_vcpu"] * dv + Q["d4_gib"] * dm) * HOURS, src)
    add("Container Apps (Dedicated D4)", "Dedicated plan management", "Dedicated Plan Management", HOURS, "hours",
        f"{mgmt} / hour", f"{mgmt} x {HOURS} h", mgmt * HOURS, src)

    # PostgreSQL
    pg, _, src = p.find("Azure_Database_for_PostgreSQL",
                        productName="Azure Database for PostgreSQL Flexible Server General Purpose Ddsv5 Series Compute",
                        skuName=f"{Q['pg_vcores']} vCore")
    st, _, _ = p.find("Azure_Database_for_PostgreSQL", productName="Azure Database for PostgreSQL Flex Server Storage",
                      meterName="Storage Data Stored")
    bk, _, _ = p.find("Azure_Database_for_PostgreSQL", meterName="Backup Storage LRS Data Stored")
    ha = 2 if Q["pg_ha"] else 1
    add("PostgreSQL Flexible Server", "Compute General Purpose Ddsv5" + (" + zone-redundant standby" if Q["pg_ha"] else ""),
        f"{Q['pg_vcores']} vCore", HOURS * ha, "hours", f"{pg} / hour", f"{pg} x {HOURS} h x {ha}", pg * HOURS * ha, src)
    add("PostgreSQL Flexible Server", "Storage", "Storage Data Stored", Q["pg_storage_gb"] * ha, "GB-month", f"{st} / GB",
        f"{Q['pg_storage_gb']} GB x {st} x {ha}", Q["pg_storage_gb"] * st * ha, src)
    add("PostgreSQL Flexible Server", "Backup storage beyond the free 100% of provisioned storage", "Backup Storage LRS",
        Q["pg_backup_extra_gb"], "GB-month", f"{bk} / GB", f"{Q['pg_backup_extra_gb']} GB x {bk}", Q["pg_backup_extra_gb"] * bk, src)

    # Redis
    rd, _, src = p.find("Redis_Cache", productName="Azure Managed Redis - Balanced", skuName=Q["redis_sku"])
    add("Azure Managed Redis", "Task queue + cache (high availability included)", f"Balanced {Q['redis_sku']}", HOURS, "hours",
        f"{rd} / hour", f"{rd} x {HOURS} h", rd * HOURS, src)

    # Search VM + disks
    vm, _, src = p.find("Virtual_Machines", armSkuName=Q["vm_sku"], productName="Virtual Machines Dsv5 Series",
                        meterName="D4s v5")
    add("Search VM (Elasticsearch + Milvus)", "Linux VM", Q["vm_sku"], HOURS, "hours", f"{vm} / hour", f"{vm} x {HOURS} h", vm * HOURS, src)
    dd, _, src2 = p.find("Storage", productName="Premium SSD Managed Disks", skuName=Q["data_disk"], meterName=f"{Q['data_disk']} Disk")
    od, _, _ = p.find("Storage", productName="Premium SSD Managed Disks", skuName=Q["os_disk"], meterName=f"{Q['os_disk']} Disk")
    add("Search VM (Elasticsearch + Milvus)", "Data disk 256 GiB", f"Premium SSD {Q['data_disk']}", 1, "disk-month", f"{dd} / month", f"1 x {dd}", dd, src2)
    add("Search VM (Elasticsearch + Milvus)", "OS disk 64 GiB", f"Premium SSD {Q['os_disk']}", 1, "disk-month", f"{od} / month", f"1 x {od}", od, src2)

    # Azure Files (documents)
    fs, _, src = p.find("Storage", productName="Files v2", skuName="Hot ZRS", meterName="Hot ZRS Data Stored")
    fw, _, _ = p.find("Storage", productName="Files v2", skuName="Hot ZRS", meterName="Hot ZRS Write Operations")
    fr, _, _ = p.find("Storage", productName="Files v2", skuName="Hot ZRS", meterName="Hot Read Operations")
    fl, _, _ = p.find("Storage", productName="Files v2", skuName="Hot ZRS", meterName="Hot ZRS List Operations")
    fmeta, _, _ = p.find("Storage", productName="Files v2", skuName="Hot ZRS", meterName="ZRS Metadata")
    add("Storage (documents)", "Azure Files Hot ZRS: data", "Hot ZRS Data Stored", Q["files_gb"], "GB-month", f"{fs} / GB",
        f"{Q['files_gb']} x {fs}", Q["files_gb"] * fs, src)
    add("Storage (documents)", "Azure Files: metadata (estimated 2% of data)", "ZRS Metadata", Q["files_gb"] * 0.02, "GB-month",
        f"{fmeta} / GB", f"{Q['files_gb']} x 0.02 x {fmeta}", Q["files_gb"] * 0.02 * fmeta, src)
    ops = Q["files_write_10k"] * fw + Q["files_read_10k"] * fr + Q["files_list_10k"] * fl
    add("Storage (documents)", "Azure Files: operations", "write / read / list per 10K",
        f"{Q['files_write_10k']}/{Q['files_read_10k']}/{Q['files_list_10k']}", "x 10K ops", f"{fw} / {fr} / {fl}",
        f"{Q['files_write_10k']} x {fw} + {Q['files_read_10k']} x {fr} + {Q['files_list_10k']} x {fl}", ops, src)

    # Front Door Premium, Zone 7
    base, _, src = p.find("Azure_Front_Door_Service_global", productName="Azure Front Door", armRegionName="Zone 7",
                          skuName="Premium", meterName="Premium Base Fees")
    rq, _, _ = p.find("Azure_Front_Door_Service_global", productName="Azure Front Door", armRegionName="Zone 7",
                      skuName="Premium", meterName="Premium Requests")
    out_, _, _ = p.find("Azure_Front_Door_Service_global", productName="Azure Front Door", armRegionName="Zone 7",
                        skuName="Premium", meterName="Premium Data Transfer Out")
    in_, _, _ = p.find("Azure_Front_Door_Service_global", productName="Azure Front Door", armRegionName="Zone 7",
                       skuName="Premium", meterName="Premium Data Transfer In")
    add("Front Door Premium + WAF (Zone 7)", "Base fee (WAF managed rules and Private Link included)", "Premium", 1, "month",
        f"{base} / month", f"1 x {base}", base, src)
    add("Front Door Premium + WAF (Zone 7)", "Requests", "Premium Requests", Q["afd_requests_10k"], "x 10K", f"{rq} / 10K",
        f"{Q['afd_requests_10k']} x {rq}", Q["afd_requests_10k"] * rq, src)
    add("Front Door Premium + WAF (Zone 7)", "Data edge -> client", "Premium Data Transfer Out", Q["afd_gb_to_client"], "GB",
        f"{out_} / GB", f"{Q['afd_gb_to_client']} x {out_}", Q["afd_gb_to_client"] * out_, src)
    add("Front Door Premium + WAF (Zone 7)", "Data edge -> origin (uploads)", "Premium Data Transfer In", Q["afd_gb_to_origin"], "GB",
        f"{in_} / GB", f"{Q['afd_gb_to_origin']} x {in_}", Q["afd_gb_to_origin"] * in_, src)

    # Registry, Key Vault, monitoring, private endpoints, backup, egress
    acr, _, src = p.find("Container_Registry", skuName="Standard", meterName="Standard Registry Unit")
    add("Platform services", "Container Registry", "Standard", Q["acr_days"], "days", f"{acr} / day", f"{Q['acr_days']} x {acr}", Q["acr_days"] * acr, src)
    kv, _, src = p.find("Key_Vault", skuName="Standard", meterName="Operations")
    add("Platform services", "Key Vault operations", "Standard", Q["kv_ops_10k"], "x 10K ops", f"{kv} / 10K", f"{Q['kv_ops_10k']} x {kv}", Q["kv_ops_10k"] * kv, src)
    la, _, src = None, None, None
    for it in json.load(open(p.folder / "Log_Analytics.json"))["items"]:
        if it["meterName"] == "Analytics Logs Data Ingestion" and it["retailPrice"] > 0:
            la, src = it["retailPrice"], json.load(open(p.folder / "Log_Analytics.json"))["query"]
    add("Platform services", "Log Analytics ingestion (5 GB/month free)", "Analytics Logs", Q["log_gb"], "GB", f"{la} / GB",
        f"max(0, {Q['log_gb']} - 5) x {la}", max(0, Q["log_gb"] - 5) * la, src)
    pe, _, src = p.find("Private_Link", armRegionName="Global", meterName="Standard Private Endpoint")
    ped, _, _ = p.find("Private_Link", armRegionName="Global", meterName="Standard Data Processed - Ingress")
    add("Platform services", "Private endpoints (Redis, Files, Key Vault)", "Standard Private Endpoint", Q["private_endpoints"],
        "endpoints", f"{pe} / hour + {ped} / GB", f"{Q['private_endpoints']} x {pe} x {HOURS} + {Q['pe_gb']} GB x {ped} x 2",
        Q["private_endpoints"] * pe * HOURS + Q["pe_gb"] * ped * 2, src)
    pi, _, src = p.find("Backup", meterName="Azure VM Protected Instance")
    fi, _, _ = p.find("Backup", meterName="Azure Files Vaulted Protected Instance")
    fz, _, _ = p.find("Backup", meterName="Azure Files Vaulted ZRS Data Stored")
    add("Backup", "Search VM protected instance", "Azure VM", Q["backup_vm_instances"], "instances", f"{pi} / month",
        f"{Q['backup_vm_instances']} x {pi}", Q["backup_vm_instances"] * pi, src)
    add("Backup", "Documents share (vaulted, ZRS)", "Azure Files Vaulted", Q["backup_files_instances"], "instance",
        f"{fi} / month + {fz} / GB", f"{fi} + {Q['backup_files_gb']} GB x {fz}", fi + Q["backup_files_gb"] * fz, src)
    eg, _, src = p.find("Bandwidth", productName="Rtn Preference: MGN", meterName="Standard Data Transfer Out",
                        tierMinimumUnits=100.0)
    add("Platform services", "Internet egress from Azure (first 100 GB free)", "Microsoft network routing", Q["egress_gb"], "GB",
        f"{eg} / GB after 100 GB", f"max(0, {Q['egress_gb']} - 100) x {eg}", max(0, Q["egress_gb"] - 100) * eg, src)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prices")
    ap.add_argument("--zar", type=float, default=None, help="USD->ZAR rate to also show rand (state its date/source)")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rows = lines(Prices(a.prices))
    total = sum(r["usd"] for r in rows)
    group = None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            print(f"\n{group}")
        print(f"  {r['item'][:58]:58} {r['sku'][:30]:30} {str(r['qty']):>10} {r['unit'][:12]:12} {r['usd']:>10,.2f}")
        print(f"      = {r['formula']}")
    print(f"\nTOTAL USD / month: {total:,.2f}" + (f"   (ZAR {total * a.zar:,.0f} at {a.zar})" if a.zar else ""))
    if a.json:
        json.dump({"rows": rows, "total_usd": round(total, 2)}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
