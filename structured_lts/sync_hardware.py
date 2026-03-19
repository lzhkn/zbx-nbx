#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# sync_hardware.py — синхронизация дисков (inventory items: serial, model, status)
# Запускается напрямую или через main.py

from common import (
    cfg, zabbix_api, netbox_api,
    ZABBIX_TAG, DISKS_ROLE,
    loging,
    select_groups, init_resources,
)


SYNC_TEMPLATES = ("Linux by Zabbix agent", "Proxmox VE by HTTP")


# --- Zabbix: шаблоны и диски ---

def get_host_templates(hostid):
    templates = zabbix_api.template.get(hostids=hostid)
    return [t["host"] for t in templates]


def get_item_value(hostid, key_pattern, default=None):
    items = zabbix_api.item.get(
        hostids=hostid,
        search={"key_": key_pattern},
        output=["key_", "lastvalue"]
    )
    for item in items:
        if key_pattern in item["key_"]:
            value = item.get("lastvalue", "").strip()
            if value and value.lower() not in ("0", "none", "null", "", "unknown"):
                return value
    return default


def extract_disk_name(key):
    import re
    match = re.search(r'\[([^\]]+)\]', key)
    return match.group(1) if match else key


def get_disk_model(hostid, disk_name, source_type):
    if source_type == "smart":
        key_pattern = f"smart.disk.model[{disk_name}]"
    elif source_type == "lsi":
        key_pattern = f"lsi.pd.model[{disk_name}]"
    else:
        return None
    return get_item_value(hostid, key_pattern)


def get_disks_from_zabbix(hostid):
    """Собирает диски хоста из Zabbix (smart.disk.sn + lsi.pd.sn)."""
    disks = {}
    patterns = [
        ("smart.disk.sn", "smart"),
        ("lsi.pd.sn",      "lsi"),
    ]

    for pattern, source_type in patterns:
        items = zabbix_api.item.get(
            hostids=hostid,
            search={"key_": pattern},
            output=["name", "key_", "lastvalue"]
        )
        for item in items:
            serial = item.get("lastvalue", "").strip()
            if not serial or serial == "0" or serial.lower() in ("none", "null", "", "unknown"):
                continue
            disk_name = extract_disk_name(item["key_"])
            model = get_disk_model(hostid, disk_name, source_type)
            if serial not in disks:
                disks[serial] = {
                    "name":   disk_name,
                    "serial": serial,
                    "model":  model,
                    "source": source_type,
                }
                loging(f"[DISK FOUND] {disk_name}: {serial}, model: {model}", "debug")

    return disks


# --- NetBox: диски ---

def get_disks_from_netbox(device_id):
    items = netbox_api.dcim.inventory_items.filter(device_id=device_id)
    return {item.serial: item for item in items if item.serial}


def sync_disks(device, zabbix_disks):
    """Синхронизирует диски: Zabbix→active+zbb, только NetBox→offline."""
    from common import ZABBIX_TAG, DISKS_ROLE  # читаем актуальные глобалы

    if not ZABBIX_TAG or not DISKS_ROLE:
        loging(f"[{device.name}] ZABBIX_TAG or DISKS_ROLE not initialized", "error")
        return

    netbox_disks   = get_disks_from_netbox(device.id)
    zabbix_serials = set(zabbix_disks.keys())
    netbox_serials = set(netbox_disks.keys())

    print(f"      Дисков в Zabbix: {len(zabbix_serials)}  в NetBox: {len(netbox_serials)}")

    for serial in zabbix_serials:
        disk_data = zabbix_disks[serial]

        if serial in netbox_disks:
            nb_disk = netbox_disks[serial]
            update_data = {}
            needs_update = False

            if nb_disk.status and nb_disk.status.value != "active":
                update_data["status"] = "active"
                needs_update = True

            if ZABBIX_TAG and ZABBIX_TAG.id not in [t.id for t in (nb_disk.tags or [])]:
                update_data["tags"] = [ZABBIX_TAG.id]
                needs_update = True

            if not nb_disk.role or nb_disk.role.id != DISKS_ROLE.id:
                update_data["role"] = DISKS_ROLE.id
                needs_update = True

            if nb_disk.name != disk_data["name"]:
                update_data["name"] = disk_data["name"]
                needs_update = True

            if disk_data["model"] and nb_disk.part_id != disk_data["model"]:
                update_data["part_id"] = disk_data["model"]
                needs_update = True

            if needs_update:
                try:
                    nb_disk.update(update_data)
                    print(f"      ~ disk {disk_data['name']} [{serial}] model={disk_data['model'] or '?'}  → updated")
                    loging(f"[{device.name}] Disk updated: {serial}, model={disk_data['model']}", "sync")
                except Exception as e:
                    print(f"      ! disk {disk_data['name']} [{serial}]  → ERROR: {e}")
                    loging(f"[{device.name}] Disk update error: {e}", "error")
            else:
                print(f"      = disk {disk_data['name']} [{serial}]  → ok (no changes)")
                loging(f"[{device.name}] Disk skip (no changes): {serial}", "debug")

        else:
            create_data = {
                "device": device.id,
                "name":   disk_data["name"][:100],
                "serial": serial,
                "status": "active",
                "tags":   [ZABBIX_TAG.id],
                "role":   DISKS_ROLE.id,
            }
            if disk_data["model"]:
                create_data["part_id"] = disk_data["model"][:100]
            try:
                netbox_api.dcim.inventory_items.create(create_data)
                print(f"      + disk {disk_data['name']} [{serial}] model={disk_data['model'] or '?'}  → created")
                loging(f"[{device.name}] Disk created: {serial}, model={disk_data['model']}", "sync")
            except Exception as e:
                print(f"      ! disk {disk_data['name']} [{serial}]  → ERROR: {e}")
                loging(f"[{device.name}] Disk create error: {e}", "error")

    for serial in (netbox_serials - zabbix_serials):
        try:
            nb_disk = netbox_disks[serial]
            nb_disk.update({"status": "offline"})
            print(f"      - disk {nb_disk.name} [{serial}]  → offline")
            loging(f"[{device.name}] Disk set offline: {serial}", "sync")
        except Exception as e:
            print(f"      ! disk [{serial}]  → offline ERROR: {e}")
            loging(f"[{device.name}] Disk offline error: {e}", "error")


def sync_device_disks(hostid):
    """Синхронизирует диски одного хоста."""
    host = zabbix_api.host.get(hostids=hostid, output=["name"])[0]
    name = host["name"].split(".")[0]

    device = netbox_api.dcim.devices.get(name=name)
    if not device:
        loging(f"[{name}] Device not found in NetBox", "error")
        return

    zabbix_disks = get_disks_from_zabbix(hostid)
    loging(f"[{name}] Disks found in Zabbix: {len(zabbix_disks)}", "debug")
    sync_disks(device, zabbix_disks)


# --- Точка входа ---

def run(groups=None):
    """
    Запускает синхронизацию дисков.
    groups — список групп (из select_groups()), если None — спрашивает интерактивно.
    """
    if groups is None:
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

    loging("=" * 50, "sync")
    loging("Start sync: hardware (disks)", "sync")

    for group in groups:
        loging(f"Processing group: {group['groupname']}", "sync")
        print(f"\n[>] Группа: {group['groupname']} ({len(group['hosts'])} хостов)")

        for host in group["hosts"]:
            hostid    = host["hostid"]
            templates = get_host_templates(hostid)

            if not any(t in templates for t in SYNC_TEMPLATES):
                continue

            loging(f"Processing host: {host['name']}", "sync")
            print(f"    - {host['name']}")
            sync_device_disks(hostid)

    loging("Done: hardware (disks)", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация дисков завершена.")


if __name__ == "__main__":
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        raise SystemExit(1)
    run()
