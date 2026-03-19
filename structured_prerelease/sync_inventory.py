#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# sync_inventory.py — синхронизация устройств (serial, platform, tags, comments)
# Запускается напрямую или через main.py

from common import (
    cfg, zabbix_api, netbox_api,
    ZABBIX_TAG, DISKS_ROLE,
    loging, compact_text, slugify,
    inject_zbx_block, extract_zbx_block_text,
    get_or_create_platform,
    select_groups, init_resources,
)


SYNC_TEMPLATES = ("Linux by Zabbix agent", "Proxmox VE by HTTP")


# --- Zabbix: данные хоста ---

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


def get_linux_host_extended(hostid):
    host = zabbix_api.host.get(
        hostids=hostid,
        selectInventory=["os_full", "serialno_a", "system"],
        output=["hostid", "name", "description"]
    )[0]

    inventory = host.get("inventory", {})

    serial_from_dmidecode = get_item_value(hostid, "dmidecode.SerialNumber")
    serial = serial_from_dmidecode or inventory.get("serialno_a", "").strip()

    platform_name = get_item_value(hostid, "os.system.product_name")
    if not platform_name:
        platform_name = inventory.get("system", "").strip() or None

    return {
        "hostname":      host["name"],
        "platform_name": platform_name,
        "serial":        serial,
        "description":   host.get("description", "").strip(),
    }


# --- Синхронизация одного устройства ---

def sync_device(hostid):
    """Обновляет serial, platform, тег zbb и ZBX-блок в comments устройства."""
    from common import ZABBIX_TAG  # читаем актуальное значение глобала

    data = get_linux_host_extended(hostid)
    name = data["hostname"].split(".")[0]

    device = netbox_api.dcim.devices.get(name=name)
    if not device:
        loging(f"[{name}] Device not found in NetBox", "error")
        return

    update_data    = {}
    changed_fields = []

    if data["serial"]:
        old_serial = (device.serial or "").strip()
        if old_serial != data["serial"]:
            update_data["serial"] = data["serial"]
            changed_fields.append(f"serial: {old_serial or '∅'} → {data['serial']}")
        else:
            print(f"      = serial [{data['serial']}]  → ok")
            loging(f"[{name}] skip serial (no changes)", "debug")

    if data["platform_name"]:
        platform = get_or_create_platform(data["platform_name"])
        if platform:
            old_platform = device.platform.name if device.platform else "∅"
            if not device.platform or device.platform.id != platform.id:
                update_data["platform"] = platform.id
                changed_fields.append(f"platform: {old_platform} → {data['platform_name']}")
            else:
                print(f"      = platform [{data['platform_name']}]  → ok")
                loging(f"[{name}] skip platform (no changes)", "debug")

    current_tags = list(device.tags) if device.tags else []
    if ZABBIX_TAG and ZABBIX_TAG.id not in [t.id for t in current_tags]:
        current_tags.append(ZABBIX_TAG.id)
        update_data["tags"] = current_tags
        changed_fields.append("tag: +zbb")

    if data["description"]:
        current_comments = (device.comments or "").strip()
        new_zbx_text     = compact_text(data["description"])
        existing_zbx     = extract_zbx_block_text(current_comments)
        if existing_zbx != new_zbx_text:
            update_data["comments"] = inject_zbx_block(current_comments, new_zbx_text)
            changed_fields.append("comments: zbx-block updated")
        else:
            print(f"      = comments [zbx-block]  → ok")
            loging(f"[{name}] skip comments/zbx-block (no changes)", "debug")

    if update_data:
        try:
            device.update(update_data)
            summary = ",  ".join(changed_fields)
            print(f"      ~ device [{name}]  {summary}  → updated")
            loging(f"[{name}] Device updated: {summary}", "sync")
        except Exception as e:
            print(f"      ! device [{name}]  → ERROR: {e}")
            loging(f"[{name}] Device update error: {e}", "error")
    else:
        print(f"      = device [{name}]  → ok (no changes)")
        loging(f"[{name}] Device skip (no changes)", "debug")


# --- Точка входа ---

def run(groups=None):
    """
    Запускает синхронизацию устройств.
    groups — список групп (из select_groups()), если None — спрашивает интерактивно.
    """
    if groups is None:
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

    loging("=" * 50, "sync")
    loging("Start sync: inventory (devices)", "sync")

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
            sync_device(hostid)

    loging("Done: inventory (devices)", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация устройств завершена.")


if __name__ == "__main__":
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        raise SystemExit(1)
    run()
