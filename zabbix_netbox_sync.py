#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# zabbix_netbox_sync v10 | Синхронизация Zabbix → NetBox | Конфиг: config_disk.ini | README: README.md


# --- Импорт ---

import os
import re
import sys
import json
import fnmatch
import datetime
import time
import configparser

import urllib3
import pynetbox
from zabbix_utils import ZabbixAPI
from proxmoxer import ProxmoxAPI

# Отключаем SSL-предупреждения (self-signed)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# --- Конфигурация ---

CONFIG_FILE = "config_disk.ini"

CONFIG_EXAMPLE = """\
Пример config_disk.ini:

    [ZABBIX]
    url   = https://zabbix.example.com
    token = your_zabbix_api_token

    [NETBOX]
    url   = https://netbox.example.com
    token = your_netbox_api_token

    [PROXMOX]
    template_id = 10517
    role_vm     = 76
    domain      = .example.com

    [KVM]
    template_id = 11301
    role_vm     = 76
    cluster     = KVM
"""


def load_config(path=CONFIG_FILE):
    """Загружает и валидирует конфигурацию из INI-файла."""
    if not os.path.exists(path):
        print(f"\n[ОШИБКА] Файл конфигурации '{path}' не найден.\n")
        print(CONFIG_EXAMPLE)
        sys.exit(1)

    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")

    missing = []
    for section, key in [("ZABBIX", "url"), ("ZABBIX", "token"),
                          ("NETBOX", "url"),  ("NETBOX", "token")]:
        if not config.has_option(section, key):
            missing.append(f"[{section}] {key}")

    if missing:
        print(f"\n[ОШИБКА] В файле '{path}' отсутствуют обязательные параметры:")
        for m in missing:
            print(f"  - {m}")
        print(f"\n{CONFIG_EXAMPLE}")
        sys.exit(1)

    # [PROXMOX]
    pve_template_id = config.get("PROXMOX", "template_id", fallback=None)
    pve_role_vm     = config.get("PROXMOX", "role_vm",     fallback=None)
    pve_domain      = config.get("PROXMOX", "domain",      fallback="")
    try:
        pve_template_id = int(pve_template_id) if pve_template_id else None
        pve_role_vm     = int(pve_role_vm)     if pve_role_vm     else None
    except ValueError as e:
        print(f"[ОШИБКА] [PROXMOX] некорректное числовое значение: {e}")
        sys.exit(1)

    # [KVM]
    kvm_template_id = config.get("KVM", "template_id", fallback=None)
    kvm_role_vm     = config.get("KVM", "role_vm",     fallback=None)
    kvm_cluster     = config.get("KVM", "cluster",     fallback="KVM")
    try:
        kvm_template_id = int(kvm_template_id) if kvm_template_id else None
        kvm_role_vm     = int(kvm_role_vm)     if kvm_role_vm     else None
    except ValueError as e:
        print(f"[ОШИБКА] [KVM] некорректное числовое значение: {e}")
        sys.exit(1)

    return {
        "zabbix_url":      config["ZABBIX"]["url"].strip(),
        "zabbix_token":    config["ZABBIX"]["token"].strip(),
        "netbox_url":      config["NETBOX"]["url"].strip(),
        "netbox_token":    config["NETBOX"]["token"].strip(),
        "pve_template_id": pve_template_id,
        "pve_role_vm":     pve_role_vm,
        "pve_domain":      pve_domain.strip(),
        "kvm_template_id": kvm_template_id,
        "kvm_role_vm":     kvm_role_vm,
        "kvm_cluster":     kvm_cluster.strip(),
    }


# --- Инициализация API ---

cfg = load_config()

zabbix_api = ZabbixAPI(cfg["zabbix_url"])
zabbix_api.login(cfg["zabbix_token"])

netbox_api = pynetbox.api(cfg["netbox_url"], cfg["netbox_token"])
netbox_api.http_session.verify = False


# --- Утилиты: slugify, compact_text, loging, zbx-блок ---

def slugify(text):
    """Преобразует строку в NetBox slug (a-z, 0-9, -, _)."""
    if not text:
        return "unknown"
    text = text.lower()
    text = re.sub(r'[^a-z0-9_-]', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')[:50]


def compact_text(text):
    """Нормализует текст: убирает пустые строки, экранирует Markdown-символы для NetBox."""
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines()]
    result = []
    for line in lines:
        if not line:
            continue
        if line.startswith("#"):
            line = "\\" + line        # экранируем заголовки Markdown
        if line.startswith(("-", "*", "+")):
            line = "\\" + line        # экранируем маркированные списки
        result.append(line)
    return "\n".join(result)


# Маркер ZBX-блока в comments устройств (только devices).
ZBX_BLOCK_MARKER = "== zabbix description =="


def build_zbx_block(text):
    """Оборачивает текст в ZBX-блок с маркерами (только для devices)."""
    if not text:
        return ""
    return f"{ZBX_BLOCK_MARKER}\n\n{text}\n\n{ZBX_BLOCK_MARKER}"


def inject_zbx_block(current_comments, new_text):
    """Вставляет или обновляет ZBX-блок в comments. Свой текст вне блока не трогает."""
    pattern = re.compile(
        rf"{re.escape(ZBX_BLOCK_MARKER)}\n.*?\n{re.escape(ZBX_BLOCK_MARKER)}",
        re.DOTALL
    )
    new_block = build_zbx_block(new_text) if new_text else ""

    if pattern.search(current_comments):
        # Блок найден — заменяем или удаляем
        result = pattern.sub(new_block, current_comments) if new_block else pattern.sub("", current_comments)
        return result.strip()

    # Блока нет — добавляем в конец (пустая строка перед блоком)
    if new_block:
        base = current_comments.strip()
        return (base + "\n\n" + new_block).strip() if base else new_block
    return current_comments


def extract_zbx_block_text(comments):
    """Извлекает содержимое ZBX-блока для сравнения. Если блока нет — пустая строка."""
    pattern = re.compile(
        rf"{re.escape(ZBX_BLOCK_MARKER)}\n(.*?)\n{re.escape(ZBX_BLOCK_MARKER)}",
        re.DOTALL
    )
    m = pattern.search(comments or "")
    return m.group(1).strip() if m else ""


def loging(data="", namefile="sync"):
    """Записывает строку в лог-файл с временной меткой (sync/error/debug)."""
    date     = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.datetime.now().strftime("%H:%M:%S")
    filename = f"{namefile}_{date}.log"
    with open(filename, "a+", encoding="utf-8") as f:
        f.write(f"{time_str} :: {data}\n")


# --- NetBox: get-or-create теги/роли/платформы, retry ---

# Глобальный кэш NetBox (заполняется в init_zabbix_resources)
ZABBIX_TAG = None   # тег "zbb" — помечает диски, пришедшие из Zabbix
DISKS_ROLE = None   # роль "Disks" для inventory items


def netbox_call_with_retry(fn, retries=3, delay=5):
    """Вызов NetBox API с retry при 502/503/504/ConnectionError/Timeout."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            err_str = str(e)
            if any(code in err_str for code in ("502", "503", "504", "ConnectionError", "Timeout")):
                print(f"  [!] NetBox недоступен (попытка {attempt}/{retries}): {err_str[:80]}")
                loging(f"[RETRY {attempt}/{retries}] NetBox error: {err_str}", "error")
                if attempt < retries:
                    time.sleep(delay)
            else:
                raise  # нестандартная ошибка — не повторяем
    raise last_exc


def get_or_create_tag(name, color="green"):
    """
    Получает существующий тег из NetBox или создаёт новый.

    Использует retry-обёртку для поиска, чтобы переждать кратковременную
    недоступность NetBox.
    """
    tag = netbox_call_with_retry(lambda: netbox_api.extras.tags.get(name=name))
    if tag:
        return tag
    try:
        tag = netbox_api.extras.tags.create(name=name, slug=slugify(name), color=color)
        loging(f"[TAG] Created tag: {name}", "sync")
        return tag
    except Exception as e:
        loging(f"[TAG CREATE ERROR] {e}", "error")
        # На случай race condition — перечитываем
        return netbox_call_with_retry(lambda: netbox_api.extras.tags.get(name=name))


def get_or_create_inventory_role(name, slug=None):
    """
    Получает или создаёт роль для inventory items в NetBox.
    Роль "Disks" используется для пометки дисков, полученных из Zabbix.
    """
    if not slug:
        slug = slugify(name)
    role = netbox_call_with_retry(lambda: netbox_api.dcim.inventory_item_roles.get(name=name))
    if role:
        return role
    try:
        role = netbox_api.dcim.inventory_item_roles.create(name=name, slug=slug, color="blue")
        loging(f"[ROLE] Created inventory role: {name}", "sync")
        return role
    except Exception as e:
        loging(f"[ROLE CREATE ERROR] {e}", "error")
        return netbox_call_with_retry(lambda: netbox_api.dcim.inventory_item_roles.get(name=name))


def get_or_create_platform(platform_name):
    """
    Получает или создаёт платформу устройства в NetBox (dcim → platforms).
    Используется для хранения model/product name железного сервера.
    """
    if not platform_name:
        return None
    platform = netbox_api.dcim.platforms.get(name=platform_name)
    if platform:
        return platform
    try:
        platform = netbox_api.dcim.platforms.create(
            name=platform_name[:100],
            slug=slugify(platform_name)
        )
        loging(f"[PLATFORM] Created {platform_name}", "sync")
        return platform
    except Exception as e:
        loging(f"[PLATFORM CREATE ERROR] {e}", "error")
        return netbox_api.dcim.platforms.get(name=platform_name)


def get_or_create_cluster_type(name):
    """
    Получает или создаёт тип кластера виртуализации в NetBox.
    Примеры типов: "Proxmox VE", "KVM".
    """
    slug = slugify(name)
    ct = netbox_api.virtualization.cluster_types.get(slug=slug)
    if ct:
        return ct
    try:
        ct = netbox_api.virtualization.cluster_types.create(name=name, slug=slug)
        loging(f"[CLUSTER TYPE] Created: {name}", "sync")
    except Exception:
        ct = netbox_api.virtualization.cluster_types.get(slug=slug)
    return ct


def init_zabbix_resources():
    """Инициализирует ZABBIX_TAG и DISKS_ROLE. При неудаче возвращает False."""
    global ZABBIX_TAG, DISKS_ROLE

    ZABBIX_TAG = get_or_create_tag("zbb", "green")
    DISKS_ROLE = get_or_create_inventory_role("Disks")

    if not ZABBIX_TAG:
        loging("Failed to initialize zbb tag", "error")
        return False
    if not DISKS_ROLE:
        loging("Failed to initialize Disks role", "error")
        return False
    return True


# --- Интерактивный выбор: режим, группы, кластеры ---

def mode_to_flags(mode_str):
    """Строковый ключ режима → (sync_devices, sync_disks, sync_pve_vms, sync_kvm_vms)."""
    return {
        "devices": (True,  False, False, False),
        "disks":   (False, True,  False, False),
        "vms":     (False, False, True,  False),
        "kvm":     (False, False, False, True),
        "all":     (True,  True,  True,  True),
    }.get(mode_str, (True, True, True, True))


def select_sync_mode():
    """Меню выбора режима синхронизации. Возвращает 4 bool-флага."""
    mode_map = {"1": "devices", "2": "disks", "3": "vms", "4": "kvm", "5": "all"}

    print("\n" + "=" * 50)
    print("  Что синхронизируем?")
    print("=" * 50)
    print("  1. Только устройства  (serial, platform, tags, comments)")
    print("  2. Только диски       (inventory items)")
    print("  3. Только VM Proxmox  (PVE QEMU + LXC → NetBox)")
    print("  4. Только VM KVM      (KVM → NetBox через Zabbix items)")
    print("  5. Всё                (устройства + диски + PVE VM + KVM VM)")
    print("=" * 50)

    while True:
        choice = input("Выберите режим [1/2/3/4/5]: ").strip()
        if choice in mode_map:
            return mode_to_flags(mode_map[choice])
        print("  [!] Введите 1, 2, 3, 4 или 5")


def apply_glob_patterns(all_groups, patterns):
    """Фильтрует список групп по glob-паттернам (fnmatch). Дубликаты убираются."""
    if not patterns:
        return all_groups
    seen = set()
    result = []
    for g in all_groups:
        name = g["groupname"]
        if name not in seen and any(fnmatch.fnmatch(name, p) for p in patterns):
            seen.add(name)
            result.append(g)
    return result


def select_groups():
    """Интерактивный выбор групп Zabbix (glob-фильтр + номера). Возвращает выбранные группы."""
    print("\nЗагружаю список групп из Zabbix...")
    all_groups_raw = zabbix_api.hostgroup.get(selectHosts=["hostid", "name"])
    all_groups = [
        {"groupname": g["name"], "hosts": g["hosts"]}
        for g in sorted(all_groups_raw, key=lambda x: x["name"])
    ]

    if not all_groups:
        print("[!] Группы не найдены в Zabbix.")
        return []

    print("\n" + "-" * 50)
    print("  Фильтр по glob-паттернам (поддерживаются * и ?)")
    print("  Несколько паттернов через запятую: Servers/*, Linux/Prod*")
    print("-" * 50)
    raw_patterns = input("  Паттерны [Enter / 'all' = все группы]: ").strip()

    if raw_patterns.lower() == "all" or raw_patterns == "":
        active_patterns = []
        print("  → Показываем все группы")
    else:
        active_patterns = [p.strip() for p in raw_patterns.split(",") if p.strip()]
        print(f"  → Применяем паттерны: {', '.join(active_patterns)}")

    filtered = apply_glob_patterns(all_groups, active_patterns)

    if not filtered:
        pat_str = ", ".join(active_patterns) if active_patterns else "(все)"
        print(f"  [!] По паттернам [{pat_str}] групп не найдено.")
        return []

    print(f"\n  Найдено групп: {len(filtered)}\n")
    for i, g in enumerate(filtered, 1):
        print(f"  {i:>3}. {g['groupname']}  ({len(g['hosts'])} хостов)")

    print("\n  Введите номера через запятую, или 'all' для всех:")
    while True:
        raw = input("  Выбор: ").strip().lower()
        if raw == "all":
            selected = filtered
            break
        try:
            indices = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if not indices:
                raise ValueError
            invalid = [i for i in indices if i < 1 or i > len(filtered)]
            if invalid:
                print(f"  [!] Некорректные номера: {invalid}. Допустимо 1–{len(filtered)}")
                continue
            selected = [filtered[i - 1] for i in indices]
            break
        except ValueError:
            print("  [!] Введите номера через запятую или 'all'")

    print(f"\n  Выбрано групп: {len(selected)}")
    for g in selected:
        print(f"    - {g['groupname']} ({len(g['hosts'])} хостов)")
    return selected


# --- Zabbix API: данные хостов и дисков ---

def get_host_templates(hostid):
    """Возвращает список имён шаблонов хоста Zabbix."""
    templates = zabbix_api.template.get(hostids=hostid)
    return [t["host"] for t in templates]


def get_item_value(hostid, key_pattern, default=None):
    """Возвращает lastvalue item по частичному ключу. Пустые/нулевые значения → default."""
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
    """Читает hostname, serial, platform_name, description хоста из Zabbix."""
    host = zabbix_api.host.get(
        hostids=hostid,
        selectInventory=["os_full", "serialno_a", "system"],
        output=["hostid", "name", "description"]
    )[0]

    inventory = host.get("inventory", {})

    # serial: dmidecode → fallback inventory
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


def extract_disk_name(key):
    """Извлекает имя диска из ключа Zabbix типа 'smart.disk.sn[sda]'."""
    match = re.search(r'\[([^\]]+)\]', key)
    return match.group(1) if match else key


def get_disk_model(hostid, disk_name, source_type):
    """Получает модель диска из Zabbix (smart → smart.disk.model, lsi → lsi.pd.model)."""
    if source_type == "smart":
        key_pattern = f"smart.disk.model[{disk_name}]"
    elif source_type == "lsi":
        key_pattern = f"lsi.pd.model[{disk_name}]"
    else:
        return None
    return get_item_value(hostid, key_pattern)


def get_disks_from_zabbix(hostid):
    """Собирает диски хоста из Zabbix (smart.disk.sn + lsi.pd.sn). Возвращает {serial: info}."""
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


# --- Синхронизация устройств и дисков ---

def get_disks_from_netbox(device_id):
    """Возвращает {serial: item} для всех inventory items устройства в NetBox."""
    items = netbox_api.dcim.inventory_items.filter(device_id=device_id)
    return {item.serial: item for item in items if item.serial}


def sync_disks(device, zabbix_disks):
    """Синхронизирует диски устройства: Zabbix→active+zbb, только NetBox→offline."""
    if not ZABBIX_TAG or not DISKS_ROLE:
        loging(f"[{device.name}] ZABBIX_TAG or DISKS_ROLE not initialized", "error")
        return

    netbox_disks   = get_disks_from_netbox(device.id)
    zabbix_serials = set(zabbix_disks.keys())
    netbox_serials = set(netbox_disks.keys())

    print(f"      Дисков в Zabbix: {len(zabbix_serials)}  в NetBox: {len(netbox_serials)}")

    # --- Сценарий 1: диски из Zabbix ---
    for serial in zabbix_serials:
        disk_data = zabbix_disks[serial]

        if serial in netbox_disks:
            # Диск уже есть в NetBox — проверяем каждое поле отдельно
            nb_disk = netbox_disks[serial]
            update_data = {}
            needs_update = False

            # Проверяем статус — должен быть active (диск виден в Zabbix)
            if nb_disk.status and nb_disk.status.value != "active":
                update_data["status"] = "active"
                needs_update = True

            # Проверяем наличие тега zbb
            if ZABBIX_TAG and ZABBIX_TAG.id not in [t.id for t in (nb_disk.tags or [])]:
                update_data["tags"] = [ZABBIX_TAG.id]
                needs_update = True

            # Проверяем роль (Disks)
            if not nb_disk.role or nb_disk.role.id != DISKS_ROLE.id:
                update_data["role"] = DISKS_ROLE.id
                needs_update = True

            # Проверяем имя устройства (например sda → /dev/sda при изменении шаблона)
            if nb_disk.name != disk_data["name"]:
                update_data["name"] = disk_data["name"]
                needs_update = True

            # Проверяем модель (part_id)
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
                # Все поля совпадают — пропускаем без API-вызова
                print(f"      = disk {disk_data['name']} [{serial}]  → ok (no changes)")
                loging(f"[{device.name}] Disk skip (no changes): {serial}", "debug")

        else:
            # Диска нет в NetBox — создаём
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

    # --- Сценарий 2: диски только в NetBox → offline ---
    for serial in (netbox_serials - zabbix_serials):
        try:
            nb_disk = netbox_disks[serial]
            nb_disk.update({"status": "offline"})
            print(f"      - disk {nb_disk.name} [{serial}]  → offline")
            loging(f"[{device.name}] Disk set offline: {serial}", "sync")
        except Exception as e:
            print(f"      ! disk [{serial}]  → offline ERROR: {e}")
            loging(f"[{device.name}] Disk offline error: {e}", "error")


def update_netbox_device(hostid, sync_devices=True, sync_disks_flag=True):
    """Обновляет device и/или диски в NetBox по данным из Zabbix."""
    data = get_linux_host_extended(hostid)
    name = data["hostname"].split(".")[0]   # берём имя без домена

    device = netbox_api.dcim.devices.get(name=name)
    if not device:
        loging(f"[{name}] Device not found in NetBox", "error")
        return

    # --- Синхронизация полей устройства (режим 1) ---
    if sync_devices:
        update_data    = {}
        changed_fields = []

        # Serial — обновляем только если реально изменился
        if data["serial"]:
            old_serial = (device.serial or "").strip()
            if old_serial != data["serial"]:
                update_data["serial"] = data["serial"]
                changed_fields.append(f"serial: {old_serial or '∅'} → {data['serial']}")
            else:
                print(f"      = serial [{data['serial']}]  → ok")
                loging(f"[{name}] skip serial (no changes)", "debug")

        # Platform — создаём/получаем объект, сравниваем по id
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

        # Тег zbb — добавляем если отсутствует (не удаляем другие теги)
        current_tags = list(device.tags) if device.tags else []
        if ZABBIX_TAG and ZABBIX_TAG.id not in [t.id for t in current_tags]:
            current_tags.append(ZABBIX_TAG.id)
            update_data["tags"] = current_tags
            changed_fields.append("tag: +zbb")

        # ZBX-блок в comments — обновляем только содержимое блока между маркерами
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

    # --- Синхронизация дисков (режим 2) ---
    if sync_disks_flag:
        zabbix_disks = get_disks_from_zabbix(hostid)
        loging(f"[{name}] Disks found in Zabbix: {len(zabbix_disks)}", "debug")
        sync_disks(device, zabbix_disks)


# --- Синхронизация VM Proxmox (PVE) ---

def get_pve_hosts_from_zabbix(template_id, allowed_hostids=None):
    """Получает список PVE-хостов с credentials из макросов Zabbix."""
    result = []
    hosts = zabbix_api.host.get(
        templateids=template_id,
        selectMacros=["macro", "value"]
    )

    if allowed_hostids is not None:
        allowed_set = {str(h) for h in allowed_hostids}
        hosts = [h for h in hosts if str(h["hostid"]) in allowed_set]

    # Дефолтные макросы из шаблона (хостовые перекрывают)
    templates = zabbix_api.template.get(
        templateids=template_id,
        selectMacros=["macro", "value"]
    )
    template_macros = {}
    for tmpl in templates:
        for m in tmpl["macros"]:
            template_macros[m["macro"]] = m["value"]

    for host in hosts:
        data = dict(template_macros)
        for m in host["macros"]:
            data[m["macro"]] = m["value"]   # макросы хоста перекрывают шаблонные

        try:
            token_id_raw = data["{$PVE.TOKEN.ID}"]
            if "!" in token_id_raw:
                user_part, token_id = token_id_raw.split("!", 1)
            else:
                user_part = token_id_raw
                token_id  = token_id_raw

            result.append({
                "zabbix_name": host["host"],
                "host":        data.get("{$PVE.URL.HOST}", ""),
                "port":        data.get("{$PVE.URL.PORT}", "8006"),
                "user":        user_part,
                "token_id":    token_id,
                "token":       data.get("{$PVE.TOKEN.SECRET}", ""),
            })
        except KeyError as e:
            loging(f"[PVE] Skipping {host['host']}: missing macro {e}", "error")

    return result


def select_pve_clusters(template_id, allowed_hostids=None):
    """Интерактивный выбор PVE-кластеров из Zabbix (glob + номера)."""
    print("\nЗагружаю PVE-кластеры из Zabbix...")
    hosts = get_pve_hosts_from_zabbix(template_id, allowed_hostids=allowed_hostids)

    if not hosts:
        if allowed_hostids is not None:
            print("[!] PVE-хосты с шаблоном не найдены в выбранных группах.")
            print("    Подсказка: убедитесь, что PVE-хосты входят в выбранные Zabbix-группы,")
            print("    или запустите режим '3 — только VM' без выбора групп.")
        else:
            print("[!] PVE-хосты с шаблоном не найдены.")
        return []

    print("\n" + "-" * 50)
    print("  Фильтр PVE-хостов по glob-паттернам (поддерживаются * и ?)")
    print("-" * 50)
    raw_patterns = input("  Паттерны [Enter / 'all' = все]: ").strip()

    if raw_patterns.lower() == "all" or raw_patterns == "":
        filtered = hosts
        print("  → Показываем все PVE-хосты")
    else:
        active_patterns = [p.strip() for p in raw_patterns.split(",") if p.strip()]
        print(f"  → Применяем паттерны: {', '.join(active_patterns)}")
        filtered = [
            h for h in hosts
            if any(fnmatch.fnmatch(h["zabbix_name"], p) for p in active_patterns)
        ]

    if not filtered:
        print("  [!] По паттернам PVE-хостов не найдено.")
        return []

    print(f"\n  Найдено PVE-хостов: {len(filtered)}\n")
    for i, h in enumerate(filtered, 1):
        print(f"  {i:>3}. {h['zabbix_name']}  ({h['host']}:{h['port']})")

    print("\n  Введите номера через запятую, или 'all' для всех:")
    while True:
        raw = input("  Выбор: ").strip().lower()
        if raw == "all":
            return filtered
        try:
            indices = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if not indices:
                raise ValueError
            invalid = [i for i in indices if i < 1 or i > len(filtered)]
            if invalid:
                print(f"  [!] Некорректные номера: {invalid}. Допустимо 1–{len(filtered)}")
                continue
            selected = [filtered[i - 1] for i in indices]
            print(f"\n  Выбрано PVE-хостов: {len(selected)}")
            for h in selected:
                print(f"    - {h['zabbix_name']} ({h['host']}:{h['port']})")
            return selected
        except ValueError:
            print("  [!] Введите номера через запятую или 'all'")


def nb_find_device(name):
    """
    Ищет устройство в NetBox сначала по короткому имени, потом по имени+домен.

    Домен берётся из config [PROXMOX] domain (например ".example.com").
    """
    device = netbox_api.dcim.devices.get(name=name)
    if device:
        return device
    domain = cfg.get("pve_domain", "")
    if domain:
        device = netbox_api.dcim.devices.get(name=f"{name}{domain}")
    return device


def get_or_create_cluster(name):
    """
    Получает или создаёт кластер виртуализации Proxmox VE в NetBox.
    Тип кластера "Proxmox VE" создаётся автоматически если его нет.
    """
    cluster = netbox_api.virtualization.clusters.get(name=name)
    if cluster:
        return cluster
    cluster_type = get_or_create_cluster_type("Proxmox VE")
    try:
        cluster = netbox_api.virtualization.clusters.create(
            name=name, type=cluster_type.id, status="active"
        )
        loging(f"[CLUSTER] Created: {name}", "sync")
    except Exception:
        cluster = netbox_api.virtualization.clusters.get(name=name)
    return cluster


def parse_mac_from_iface(iface_str):
    """
    Извлекает MAC-адрес из строки конфига сетевого интерфейса PVE.

    PVE хранит конфиги сетевых интерфейсов в виде строк:
    "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,firewall=1"
    Ключ до '=' — тип сетевой карты (virtio/e1000/e1000e/rtl8139/vmxnet3).
    """
    for part in iface_str.split(","):
        key, _, val = part.partition("=")
        if key.strip() in ("virtio", "e1000e", "e1000", "rtl8139", "vmxnet3"):
            return val.strip()
    return None


def vm_pve_status_to_nb(pve_status, is_template):
    """Конвертирует статус PVE VM в NetBox: running→active, stopped→offline, paused→planned."""
    if is_template:
        return "staged"
    return {"running": "active", "stopped": "offline", "paused": "planned"}.get(
        pve_status, "failed"
    )


def parse_disk_size_mb(size_str):
    """Парсит строку размера диска PVE ('32G', '512M', '2T') в мегабайты."""
    s = size_str.strip()
    if s.endswith("T"): return int(s[:-1]) * 1_000_000
    if s.endswith("G"): return int(s[:-1]) * 1_000
    if s.endswith("M"): return int(s[:-1])
    return int(s)


def parse_lxc_disks(config_lxc, node_name):
    """Извлекает диски (rootfs, mp*) из конфига LXC. Возвращает [{path, size}, ...]."""
    disks = []
    for key, val in config_lxc.items():
        if key != "rootfs" and not key.startswith("mp"):
            continue
        path = f"{node_name}/{val.split(',')[0]}"
        size = 0
        for part in val.split(","):
            if part.startswith("size="):
                try:
                    size = parse_disk_size_mb(part.split("=", 1)[1])
                except Exception:
                    size = 0
        disks.append({"path": path, "size": size})
    return disks


def parse_lxc_interfaces(config_lxc):
    """Извлекает net-интерфейсы (net0, net1...) из конфига LXC. Возвращает [{name, mac, enabled}]."""
    interfaces = []
    for key, val in config_lxc.items():
        if not key.startswith("net"):
            continue
        mac = None
        for part in val.split(","):
            k, _, v = part.partition("=")
            if k.strip() == "hwaddr":
                mac = v.strip()
                break
        enabled = "link_down" not in val
        interfaces.append({"name": key, "mac": mac, "enabled": enabled})
    return interfaces


def parse_vm_disks(config_vm, node_name):
    """Извлекает диски (scsi*, ide*) из конфига QEMU VM. CD-ROM пропускает."""
    disks = []
    for key, val in config_vm.items():
        is_disk = ("scsi" in key and key != "scsihw") or "ide" in key
        if not is_disk or "cdrom" in val:
            continue
        path = f"{node_name}/{val.split(',')[0]}"
        size = 0
        for part in val.split(","):
            if part.startswith("size="):
                try:
                    size = parse_disk_size_mb(part.split("=", 1)[1])
                except Exception:
                    size = 0
        disks.append({"path": path, "size": size})
    return disks


def parse_vm_interfaces(config_vm):
    """Извлекает net-интерфейсы (net0, net1...) из конфига QEMU VM."""
    interfaces = []
    for key, val in config_vm.items():
        if key.startswith("net"):
            interfaces.append({
                "name":    key,
                "mac":     parse_mac_from_iface(val),
                "enabled": "link_down" not in val,
            })
    return interfaces


def _assign_mac(nb_iface, mac, all_macs_cache):
    """Создаёт/переназначает MAC на VMinterface и ставит как primary_mac_address."""
    if mac in all_macs_cache:
        mac_obj = netbox_api.dcim.mac_addresses.get(mac_address=mac)
        if mac_obj and mac_obj.assigned_object and mac_obj.assigned_object["id"] == nb_iface.id:
            return  # MAC уже правильно назначен — ничего не делаем
        if mac_obj:
            mac_obj.delete()  # MAC занят другим объектом — освобождаем

    try:
        mac_obj = netbox_api.dcim.mac_addresses.create({
            "mac_address":          mac,
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id":   nb_iface.id,
        })
        all_macs_cache.add(mac)
        loging(f"[MAC] {mac} → iface {nb_iface.name}", "sync")
    except Exception as e:
        loging(f"[MAC] Create error {mac}: {e}", "error")
        return

    # Ставим как primary MAC если не задан
    iface_fresh = netbox_api.virtualization.interfaces.get(id=nb_iface.id)
    if iface_fresh and iface_fresh.primary_mac_address is None:
        iface_fresh.primary_mac_address = {"id": mac_obj.id}
        try:
            iface_fresh.save()
        except Exception as e:
            loging(f"[MAC] primary_mac_address error: {e}", "error")


def sync_vm_disks_nb(nb_vm, pve_disks):
    """Синхронизирует virtual_disks VM: PVE→создать/обновить, только NB→удалить."""
    pve_paths    = {d["path"] for d in pve_disks}
    nb_vm_disks  = {
        d.name: d
        for d in netbox_api.virtualization.virtual_disks.filter(virtual_machine_id=nb_vm.id)
    }

    print(f"      Дисков в PVE: {len(pve_disks)}  в NetBox: {len(nb_vm_disks)}")

    for disk in pve_disks:
        size_gb = f"{disk['size'] // 1000}G" if disk["size"] >= 1000 else f"{disk['size']}M"
        if disk["path"] not in nb_vm_disks:
            try:
                netbox_api.virtualization.virtual_disks.create({
                    "virtual_machine": nb_vm.id,
                    "name":  disk["path"],
                    "size":  disk["size"],
                })
                print(f"      + disk {disk['path']} ({size_gb})  → created")
                loging(f"[{nb_vm.name}] Disk created: {disk['path']}", "sync")
            except Exception as e:
                print(f"      ! disk {disk['path']}  → ERROR: {e}")
                loging(f"[{nb_vm.name}] Disk create error: {e}", "error")
        else:
            nb_disk = nb_vm_disks[disk["path"]]
            if nb_disk.size != disk["size"]:
                try:
                    nb_disk.update({"size": disk["size"]})
                    print(f"      ~ disk {disk['path']} ({size_gb})  → size updated")
                    loging(f"[{nb_vm.name}] Disk size updated: {disk['path']}", "sync")
                except Exception as e:
                    print(f"      ! disk {disk['path']}  → update ERROR: {e}")
                    loging(f"[{nb_vm.name}] Disk update error: {e}", "error")
            else:
                print(f"      = disk {disk['path']} ({size_gb})  → ok")
                loging(f"[{nb_vm.name}] Disk skip (ok): {disk['path']}", "debug")

    for name, nb_disk in nb_vm_disks.items():
        if name not in pve_paths:
            try:
                nb_disk.delete()
                print(f"      - disk {name}  → deleted")
                loging(f"[{nb_vm.name}] Disk deleted: {name}", "sync")
            except Exception as e:
                print(f"      ! disk {name}  → delete ERROR: {e}")
                loging(f"[{nb_vm.name}] Disk delete error: {e}", "error")


def sync_vm_interfaces_nb(nb_vm, pve_ifaces, all_macs_cache):
    """
    Синхронизирует сетевые интерфейсы VM в NetBox (PVE QEMU и LXC).

    Поведение:
      - Интерфейс есть в PVE, нет в NB → создаём + назначаем MAC
      - Интерфейс есть в обоих         → обновляем enabled если изменился + MAC
      - Интерфейс только в NB          → удаляем (убрали из конфига VM)
    """
    pve_names = {i["name"] for i in pve_ifaces}
    nb_ifaces = {
        i.name: i
        for i in netbox_api.virtualization.interfaces.filter(virtual_machine_id=nb_vm.id)
    }

    for iface in pve_ifaces:
        if iface["name"] not in nb_ifaces:
            try:
                nb_iface = netbox_api.virtualization.interfaces.create({
                    "virtual_machine": nb_vm.id,
                    "name":    iface["name"],
                    "enabled": iface["enabled"],
                })
                loging(f"[{nb_vm.name}] Interface created: {iface['name']}", "sync")
                if iface["mac"]:
                    _assign_mac(nb_iface, iface["mac"], all_macs_cache)
            except Exception as e:
                loging(f"[{nb_vm.name}] Interface create error: {e}", "error")
        else:
            nb_iface = nb_ifaces[iface["name"]]
            if nb_iface.enabled != iface["enabled"]:
                try:
                    nb_iface.update({"enabled": iface["enabled"]})
                except Exception as e:
                    loging(f"[{nb_vm.name}] Interface update error: {e}", "error")
            if iface["mac"]:
                _assign_mac(nb_iface, iface["mac"], all_macs_cache)

    for name, nb_iface in nb_ifaces.items():
        if name not in pve_names:
            try:
                nb_iface.delete()
                loging(f"[{nb_vm.name}] Interface deleted: {name}", "sync")
            except Exception as e:
                loging(f"[{nb_vm.name}] Interface delete error: {e}", "error")


def sync_pve_cluster(cluster_info, allowed_nodes=None):
    """
    Синхронизирует один PVE-кластер (или standalone-ноду) с NetBox.

    Шаги:
      1. Подключение к ProxmoxAPI по credentials из cluster_info
      2. Определение имени кластера (из cluster.status или hostname ноды)
      3. Создание/получение кластера в NetBox
      4. Привязка физических нод к кластеру в NetBox (device.cluster)
      5. Обход всех нод: обработка QEMU VM и LXC-контейнеров
      6. Удаление из NetBox VM, которых нет в PVE (только по обработанным нодам)

    Args:
        cluster_info:   dict с параметрами подключения (из select_pve_clusters)
        allowed_nodes:  set коротких имён нод для обработки (None = все)
    """
    role_vm_id = cfg.get("pve_role_vm")
    loging(f"[PVE] Start: {cluster_info['zabbix_name']}", "sync")
    print(f"\n[PVE] Кластер: {cluster_info['zabbix_name']} ({cluster_info['host']})")
    if allowed_nodes:
        print(f"  Фильтр нод: {', '.join(sorted(allowed_nodes))}")

    # Подключаемся к Proxmox API
    try:
        proxmox = ProxmoxAPI(
            host=cluster_info["host"],
            port=int(cluster_info["port"]),
            user=cluster_info["user"],
            token_name=cluster_info["token_id"],
            token_value=cluster_info["token"],
            verify_ssl=False,
            service="PVE"
        )
    except Exception as e:
        loging(f"[PVE] Connection error {cluster_info['zabbix_name']}: {e}", "error")
        print(f"  [!] Ошибка подключения: {e}")
        return

    # Имя кластера: из cluster.status если это настоящий кластер,
    # иначе короткое hostname ноды (standalone)
    try:
        cluster_status = proxmox.cluster.status.get()
    except Exception as e:
        loging(f"[PVE] cluster.status.get() failed {cluster_info['zabbix_name']}: {e}", "error")
        print(f"  [!] Не удалось получить статус кластера (таймаут или недоступен): {e}")
        print(f"  [!] Кластер {cluster_info['zabbix_name']} пропущен.")
        return

    # Определяем имя кластера и является ли это настоящим PVE-кластером
    cluster_name = cluster_info["zabbix_name"].split(".")[0]
    is_real_cluster = False
    for entry in cluster_status:
        if entry["type"] == "cluster":
            cluster_name = entry["name"]
            is_real_cluster = True
            break

    # Если это настоящий PVE-кластер — обходим ВСЕ ноды через точку входа.
    # allowed_nodes нужен только для standalone-нод (когда выбрали не все из списка).
    effective_allowed_nodes = None if is_real_cluster else allowed_nodes

    if is_real_cluster:
        print(f"  Режим: PVE-кластер '{cluster_name}' — обходим все ноды")
    else:
        print(f"  Режим: standalone-нода")

    nb_cluster = get_or_create_cluster(cluster_name)
    loging(f"[PVE] NetBox cluster: {cluster_name} id={nb_cluster.id} real_cluster={is_real_cluster}", "sync")

    # Привязываем все ноды кластера к кластеру в NetBox
    for entry in cluster_status:
        if entry["type"] == "node":
            device = nb_find_device(entry["name"])
            if device:
                if not device.cluster or device.cluster.id != nb_cluster.id:
                    device.cluster = {"id": nb_cluster.id}
                    try:
                        device.save()
                        loging(f"[PVE] Node {entry['name']} → {cluster_name}", "sync")
                    except Exception as e:
                        loging(f"[PVE] Node bind error {entry['name']}: {e}", "error")
            else:
                loging(f"[PVE] Node not in NetBox: {entry['name']}", "error")

    # Один запрос для получения всех MAC-адресов кластера (кэш для _assign_mac)
    all_macs_cache = {str(m.mac_address) for m in netbox_api.dcim.mac_addresses.all()}
    pve_vm_names   = set()

    try:
        nodes = proxmox.nodes.get()
    except Exception as e:
        loging(f"[PVE] nodes.get() failed {cluster_info['zabbix_name']}: {e}", "error")
        print(f"  [!] Не удалось получить список нод: {e}")
        return

    for node in nodes:
        node_name = node["node"]

        # Для standalone: пропускаем ноды не из выбранного списка
        if effective_allowed_nodes and node_name not in effective_allowed_nodes:
            loging(f"[PVE] Node not in selection, skip: {node_name}", "sync")
            print(f"  [~] Нода пропущена (не выбрана): {node_name}")
            continue

        if node["status"] != "online":
            loging(f"[PVE] Node offline, skip: {node_name}", "sync")
            continue

        # ── QEMU VM ──────────────────────────────────────────────────────────
        try:
            all_vms_on_node = proxmox.nodes(node_name).qemu.get()
        except Exception as e:
            loging(f"[PVE] qemu.get() failed on {node_name}: {e}", "error")
            print(f"  [!] Нода {node_name}: не удалось получить список VM, пропускаем: {e}")
            continue
        print(f"  [>] Нода: {node_name} — QEMU VM: {len(all_vms_on_node)}")
        for _vm in all_vms_on_node:
            print(f"       vmid={_vm['vmid']} name={_vm.get('name','?')} "
                  f"status={_vm['status']} template={_vm.get('template',0)}")

        for vm in all_vms_on_node:
            try:
                config_vm = proxmox.nodes(node_name).qemu(vm["vmid"]).config.get()
            except Exception as e:
                loging(f"[PVE] config.get error vmid={vm['vmid']}: {e}", "error")
                continue

            is_template = bool(vm.get("template", 0))
            vm_nb_name  = f"{node_name}/{vm['vmid']}/{config_vm['name']}"
            pve_vm_names.add(vm_nb_name)

            pve_disks  = parse_vm_disks(config_vm, node_name)
            pve_ifaces = parse_vm_interfaces(config_vm)
            nb_status  = vm_pve_status_to_nb(vm["status"], is_template)
            host_dev   = nb_find_device(node_name)

            nb_vm = netbox_api.virtualization.virtual_machines.get(name=vm_nb_name)

            if nb_vm is None:
                # --- Создание VM ---
                create_data = {
                    "name":    vm_nb_name,
                    "cluster": nb_cluster.id,
                    "status":  nb_status,
                    "vcpus":   config_vm.get("cores", 1),
                    "memory":  config_vm.get("memory", 0),
                }
                if role_vm_id: create_data["role"]   = role_vm_id
                if host_dev:   create_data["device"] = host_dev.id
                if "description" in config_vm:
                    create_data["comments"] = compact_text(config_vm["description"])

                tags = []
                if ZABBIX_TAG:
                    tags.append(ZABBIX_TAG.id)
                for tag_name in config_vm.get("tags", "").split(";"):
                    tag_name = tag_name.strip()
                    if tag_name:
                        t = get_or_create_tag(tag_name)
                        if t: tags.append(t.id)
                if tags: create_data["tags"] = tags

                try:
                    nb_vm = netbox_api.virtualization.virtual_machines.create(create_data)
                    loging(f"[{node_name}] VM created: {vm_nb_name}", "sync")
                    print(f"    + {vm_nb_name}")
                except Exception as e:
                    loging(f"[{node_name}] VM create error {vm_nb_name}: {e}", "error")
                    continue
            else:
                # --- Обновление VM (только изменившиеся поля) ---
                changed = False
                changed_fields = []

                if nb_vm.status and nb_vm.status.value != nb_status:
                    nb_vm.status = nb_status; changed = True
                    changed_fields.append(f"status→{nb_status}")
                else:
                    loging(f"[{node_name}] VM skip status (ok): {vm_nb_name}", "debug")

                if nb_vm.vcpus != config_vm.get("cores", 1):
                    nb_vm.vcpus = config_vm.get("cores", 1); changed = True
                    changed_fields.append("vcpus")
                else:
                    loging(f"[{node_name}] VM skip vcpus (ok): {vm_nb_name}", "debug")

                if nb_vm.memory != int(config_vm.get("memory", 0)):
                    nb_vm.memory = config_vm.get("memory", 0); changed = True
                    changed_fields.append("memory")
                else:
                    loging(f"[{node_name}] VM skip memory (ok): {vm_nb_name}", "debug")

                if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                    nb_vm.role = role_vm_id; changed = True
                    changed_fields.append("role")

                if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                    nb_vm.device = host_dev.id; changed = True
                    changed_fields.append("device")

                # comments — обновляем если изменился текст описания (без zbx-блока для VM)
                descr = compact_text(config_vm.get("description", ""))
                if descr:
                    cur_comments = (nb_vm.comments or "").strip()
                    if cur_comments != descr:
                        nb_vm.comments = descr
                        changed = True; changed_fields.append("comments")
                    else:
                        loging(f"[{node_name}] VM skip comments (ok): {vm_nb_name}", "debug")

                # Теги — добавляем новые, не удаляем существующие
                current_tag_names = {t["name"] for t in (nb_vm.tags or [])}
                if ZABBIX_TAG and ZABBIX_TAG.name not in current_tag_names:
                    nb_vm.tags = list(nb_vm.tags or []) + [ZABBIX_TAG.id]
                    changed = True; changed_fields.append("tag+zbb")
                for tag_name in config_vm.get("tags", "").split(";"):
                    tag_name = tag_name.strip()
                    if tag_name and tag_name not in current_tag_names:
                        t = get_or_create_tag(tag_name)
                        if t:
                            nb_vm.tags = list(nb_vm.tags or []) + [t.id]
                            changed = True; changed_fields.append(f"tag+{tag_name}")

                if changed:
                    try:
                        nb_vm.save()
                        loging(f"[{node_name}] VM updated ({', '.join(changed_fields)}): {vm_nb_name}", "sync")
                        print(f"    ~ {vm_nb_name}  [{', '.join(changed_fields)}]")
                    except Exception as e:
                        loging(f"[{node_name}] VM update error {vm_nb_name}: {e}", "error")
                else:
                    print(f"    = {vm_nb_name}  → ok (no changes)")
                    loging(f"[{node_name}] VM skip (no changes): {vm_nb_name}", "debug")

            sync_vm_disks_nb(nb_vm, pve_disks)
            sync_vm_interfaces_nb(nb_vm, pve_ifaces, all_macs_cache)

        # ── LXC-контейнеры ───────────────────────────────────────────────────
        try:
            all_cts_on_node = proxmox.nodes(node_name).lxc.get()
        except Exception as e:
            loging(f"[PVE] lxc.get() failed on {node_name}: {e}", "error")
            print(f"  [!] Нода {node_name}: не удалось получить список LXC, пропускаем: {e}")
            continue
        for ct in all_cts_on_node:
            config_ct   = proxmox.nodes(node_name).lxc(ct["vmid"]).config.get()
            is_template = bool(ct.get("template", 0))
            ct_nb_name  = f"{node_name}/{ct['vmid']}/{ct['name']}"
            pve_vm_names.add(ct_nb_name)

            pve_disks  = parse_lxc_disks(config_ct, node_name)
            pve_ifaces = parse_lxc_interfaces(config_ct)
            nb_status  = vm_pve_status_to_nb(ct["status"], is_template)
            host_dev   = nb_find_device(node_name)

            nb_vm = netbox_api.virtualization.virtual_machines.get(name=ct_nb_name)

            if nb_vm is None:
                create_data = {
                    "name":    ct_nb_name,
                    "cluster": nb_cluster.id,
                    "status":  nb_status,
                    "vcpus":   config_ct.get("cores", 1),
                    "memory":  config_ct.get("memory", 0),
                }
                if role_vm_id: create_data["role"]   = role_vm_id
                if host_dev:   create_data["device"] = host_dev.id
                if "description" in config_ct:
                    create_data["comments"] = compact_text(config_ct["description"])

                tags = []
                if ZABBIX_TAG:
                    tags.append(ZABBIX_TAG.id)
                for tag_name in config_ct.get("tags", "").split(";"):
                    tag_name = tag_name.strip()
                    if tag_name:
                        t = get_or_create_tag(tag_name)
                        if t: tags.append(t.id)
                if tags: create_data["tags"] = tags

                try:
                    nb_vm = netbox_api.virtualization.virtual_machines.create(create_data)
                    loging(f"[{node_name}] LXC created: {ct_nb_name}", "sync")
                    print(f"    + LXC {ct_nb_name}")
                except Exception as e:
                    loging(f"[{node_name}] LXC create error {ct_nb_name}: {e}", "error")
                    continue
            else:
                changed = False
                changed_fields = []

                if nb_vm.status and nb_vm.status.value != nb_status:
                    nb_vm.status = nb_status; changed = True
                    changed_fields.append(f"status→{nb_status}")
                else:
                    loging(f"[{node_name}] LXC skip status (ok): {ct_nb_name}", "debug")

                if nb_vm.vcpus != config_ct.get("cores", 1):
                    nb_vm.vcpus = config_ct.get("cores", 1); changed = True
                    changed_fields.append("vcpus")
                else:
                    loging(f"[{node_name}] LXC skip vcpus (ok): {ct_nb_name}", "debug")

                if nb_vm.memory != int(config_ct.get("memory", 0)):
                    nb_vm.memory = config_ct.get("memory", 0); changed = True
                    changed_fields.append("memory")
                else:
                    loging(f"[{node_name}] LXC skip memory (ok): {ct_nb_name}", "debug")

                if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                    nb_vm.role = role_vm_id; changed = True
                    changed_fields.append("role")

                if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                    nb_vm.device = host_dev.id; changed = True
                    changed_fields.append("device")

                descr = compact_text(config_ct.get("description", ""))
                if descr:
                    cur_comments = (nb_vm.comments or "").strip()
                    if cur_comments != descr:
                        nb_vm.comments = descr
                        changed = True; changed_fields.append("comments")
                    else:
                        loging(f"[{node_name}] LXC skip comments (ok): {ct_nb_name}", "debug")

                # Тег zbb — добавляем если отсутствует
                current_tag_names = {t["name"] for t in (nb_vm.tags or [])}
                if ZABBIX_TAG and ZABBIX_TAG.name not in current_tag_names:
                    nb_vm.tags = list(nb_vm.tags or []) + [ZABBIX_TAG.id]
                    changed = True; changed_fields.append("tag+zbb")

                if changed:
                    try:
                        nb_vm.save()
                        loging(f"[{node_name}] LXC updated ({', '.join(changed_fields)}): {ct_nb_name}", "sync")
                        print(f"    ~ LXC {ct_nb_name}  [{', '.join(changed_fields)}]")
                    except Exception as e:
                        loging(f"[{node_name}] LXC update error {ct_nb_name}: {e}", "error")
                else:
                    print(f"    = LXC {ct_nb_name}  → ok (no changes)")
                    loging(f"[{node_name}] LXC skip (no changes): {ct_nb_name}", "debug")

            sync_vm_disks_nb(nb_vm, pve_disks)
            sync_vm_interfaces_nb(nb_vm, pve_ifaces, all_macs_cache)

    # Удаляем из NetBox VM, которых нет в PVE.
    # Для кластера — удаляем по всем нодам (обошли все).
    # Для standalone — только по нодам, которые обрабатывались.
    for nb_vm in netbox_api.virtualization.virtual_machines.filter(cluster_id=nb_cluster.id):
        if nb_vm.name in pve_vm_names:
            continue
        vm_node = nb_vm.name.split("/")[0] if "/" in nb_vm.name else ""
        if effective_allowed_nodes and vm_node not in effective_allowed_nodes:
            continue  # standalone: нода не обрабатывалась — не трогаем
        try:
            nb_vm.delete()
            loging(f"[PVE] VM deleted: {nb_vm.name}", "sync")
            print(f"    - удалена: {nb_vm.name}")
        except Exception as e:
            loging(f"[PVE] VM delete error {nb_vm.name}: {e}", "error")

    loging(f"[PVE] Done: {cluster_name}", "sync")


# --- Синхронизация VM KVM ---
#
# ─────────────────────────────────────────────────────────────────────────────
# Архитектура шаблона "Tempalte KVM":
#
#  RAW items (мастер-items, тип TEXT, lastvalue = JSON):
#    vmstatus          — статусы всех VM на гипервизоре
#    vmstatistic_cpu_mem — CPU и память всех VM
#    vm_blk_discovery  — диски всех VM (для LLD discovery дисков)
#    vmlist_network    — сетевые интерфейсы всех VM (для LLD discovery сети)
#
#  Dependent items (создаются LLD discovery из мастер-items):
#    vmstatus.name             — LLD discovery rule (из vmstatus)
#    vmstatus.status[VMNAME]   — статус конкретной VM (строка "running"/"shut off")
#    disk.Capacity[VMNAME,TGT] — размер диска в байтах
#    vmlist.MAC[VMNAME,NET]    — MAC-адрес интерфейса
#    ... и др. (метрики CPU, RAM, сети — для мониторинга, не для инвентаризации)
#
# ─────────────────────────────────────────────────────────────────────────────
# Что мы читаем и откуда:
#
#  vmstatus (raw JSON) → список VM и их статусов
#    {"data": [{"VMNAME": "myvm", "STATUS": "running"}, ...]}
#
#    ВАЖНО: в Zabbix вы видите dependent items vmstatus.status[myvm] = "running",
#    но для инвентаризации мы читаем именно мастер-item vmstatus (один запрос
#    для всех VM сразу, без N запросов по одному). Если vmstatus пустой —
#    значит шаблон ещё не собрал данные (нужно подождать или проверить скрипт
#    сбора данных на гипервизоре).
#
#  vmstatistic_cpu_mem (raw JSON) → CPU и RAM
#    {"data": [{"VMNAME": "myvm", "actual": 4294967296, "nrVirtCpu": 4}, ...]}
#    actual    = полный объём ОЗУ VM в байтах → конвертируем в МБ
#    nrVirtCpu = количество vCPU
#
#  vm_blk_discovery (raw JSON) → диски
#    {"data": [{"VMNAME": "myvm", "Target": "vda",
#               "Source": "/var/lib/libvirt/images/myvm.qcow2",
#               "Device": "disk", "Type": "file"}, ...]}
#    cdrom и floppy пропускаем.
#    Размер читаем через dependent item disk.Capacity[VMNAME,TARGET] (байты).
#
#  vmlist_network (raw JSON) → сетевые интерфейсы
#    {"data": [{"VMNAME": "myvm", "Interface": "vnet0",
#               "MAC": "aa:bb:cc:dd:ee:ff", "Model": "virtio",
#               "Source": "br0", "Type": "bridge"}, ...]}
#    Interface == "-1" = нет сети (фильтруется как в LLD шаблона).
#
# ─────────────────────────────────────────────────────────────────────────────
# Имя VM в NetBox: "<node_shortname>/<VMNAME>"
# (у KVM нет vmid как в PVE — используем только имя)
#
# ==============================================================================

def get_kvm_hosts_from_zabbix(template_id, allowed_hostids=None):
    """
    Получает список KVM-гипервизоров из Zabbix по ID шаблона (11301).

    Returns:
        list[dict]: [{zabbix_name, hostid, display}, ...]
    """
    hosts = zabbix_api.host.get(templateids=template_id, output=["hostid", "host", "name"])
    if allowed_hostids is not None:
        allowed_set = {str(h) for h in allowed_hostids}
        hosts = [h for h in hosts if str(h["hostid"]) in allowed_set]
    return [
        {"zabbix_name": h["host"], "hostid": h["hostid"], "display": h["name"]}
        for h in hosts
    ]


def select_kvm_hosts(template_id, allowed_hostids=None):
    """
    Интерактивный выбор KVM-гипервизоров (аналог select_pve_clusters).

    Returns:
        list[dict]: выбранные хосты
    """
    print("\nЗагружаю KVM-хосты из Zabbix...")
    hosts = get_kvm_hosts_from_zabbix(template_id, allowed_hostids=allowed_hostids)

    if not hosts:
        if allowed_hostids is not None:
            print("[!] KVM-хосты с шаблоном не найдены в выбранных группах.")
        else:
            print("[!] KVM-хосты с шаблоном не найдены.")
        return []

    print("\n" + "-" * 50)
    print("  Фильтр KVM-хостов по glob-паттернам (поддерживаются * и ?)")
    print("-" * 50)
    raw_patterns = input("  Паттерны [Enter / 'all' = все]: ").strip()

    if raw_patterns.lower() == "all" or raw_patterns == "":
        filtered = hosts
        print("  → Показываем все KVM-хосты")
    else:
        active_patterns = [p.strip() for p in raw_patterns.split(",") if p.strip()]
        print(f"  → Применяем паттерны: {', '.join(active_patterns)}")
        filtered = [h for h in hosts if any(fnmatch.fnmatch(h["zabbix_name"], p) for p in active_patterns)]

    if not filtered:
        print("  [!] По паттернам KVM-хостов не найдено.")
        return []

    print(f"\n  Найдено KVM-хостов: {len(filtered)}\n")
    for i, h in enumerate(filtered, 1):
        print(f"  {i:>3}. {h['zabbix_name']}  ({h['display']})")

    print("\n  Введите номера через запятую, или 'all' для всех:")
    while True:
        raw = input("  Выбор: ").strip().lower()
        if raw == "all":
            return filtered
        try:
            indices = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if not indices:
                raise ValueError
            invalid = [i for i in indices if i < 1 or i > len(filtered)]
            if invalid:
                print(f"  [!] Некорректные номера: {invalid}. Допустимо 1–{len(filtered)}")
                continue
            selected = [filtered[i - 1] for i in indices]
            print(f"\n  Выбрано KVM-хостов: {len(selected)}")
            for h in selected:
                print(f"    - {h['zabbix_name']}")
            return selected
        except ValueError:
            print("  [!] Введите номера через запятую или 'all'")


def get_kvm_raw_item(hostid, item_key):
    """
    Читает lastvalue RAW-item (тип TEXT) из Zabbix по точному ключу и парсит JSON.

    Используем filter= (точное совпадение ключа), а не search= (LIKE).
    Это важно: мастер-items шаблона KVM имеют точные ключи без параметров
    (vmstatus, vm_blk_discovery, и т.д.), и search мог бы вернуть
    dependent items с похожими ключами (vmstatus.status[...]).

    Возвращает распарсенный dict/list или None при ошибке/пустом значении.
    """
    items = zabbix_api.item.get(
        hostids=hostid,
        filter={"key_": item_key},
        output=["key_", "lastvalue"]
    )
    if not items:
        loging(f"[KVM] item not found: hostid={hostid} key={item_key}", "error")
        return None
    val = items[0].get("lastvalue", "").strip()
    if not val or val in ("0", "", "null", "none"):
        loging(f"[KVM] item empty: key={item_key}", "debug")
        return None
    try:
        return json.loads(val)
    except Exception as e:
        loging(f"[KVM] JSON parse error key={item_key}: {e} | val[:120]={val[:120]}", "error")
        return None


def get_kvm_dependent_value(hostid, item_key):
    """
    Читает lastvalue одного dependent item из Zabbix по точному ключу.

    Используется для disk.Capacity[VMNAME,TARGET] и аналогичных
    dependent items с параметрами в ключе.
    Возвращает строку или None.
    """
    items = zabbix_api.item.get(
        hostids=hostid,
        filter={"key_": item_key},
        output=["key_", "lastvalue"]
    )
    if not items:
        return None
    val = items[0].get("lastvalue", "").strip()
    return val if val and val not in ("0", "", "null") else None


def kvm_status_to_nb(status_str):
    """Конвертирует статус KVM VM в NetBox: running→active, shut off→offline, paused→planned."""
    return {
        "running":     "active",
        "shut off":    "offline",
        "shut":        "offline",
        "shutdown":    "offline",
        "in shutdown": "offline",
        "paused":      "planned",
        "crashed":     "failed",
        "pmsuspended": "planned",
        "idle":        "planned",
    }.get((status_str or "").strip().lower(), "offline")


def get_or_create_kvm_cluster(cluster_name):
    """Получает или создаёт KVM-кластер в NetBox (тип KVM создаётся автоматически)."""
    cluster = netbox_api.virtualization.clusters.get(name=cluster_name)
    if cluster:
        return cluster
    cluster_type = get_or_create_cluster_type("KVM")
    try:
        cluster = netbox_api.virtualization.clusters.create(
            name=cluster_name, type=cluster_type.id, status="active"
        )
        loging(f"[KVM] Cluster created: {cluster_name}", "sync")
    except Exception:
        cluster = netbox_api.virtualization.clusters.get(name=cluster_name)
    return cluster


def parse_kvm_vm_list(hostid, node_name):
    """
    Читает список VM и их статусы через dependent items vmstatus.status[<VMNAME>].

    Шаблон создаёт по одному dependent item на каждую VM через LLD discovery:
        vmstatus.status[myvm]    → lastvalue = "running"
        vmstatus.status[myvm2]  → lastvalue = "shut off"

    Именно эти items видны в Zabbix Latest Data.
    Имя VM извлекается из ключа item: vmstatus.status[<VMNAME>] → VMNAME.
    Имя VM становится именем виртуальной машины в NetBox.

    Поиск через search={"key_": "vmstatus.status["} находит все items
    с ключами начинающимися на "vmstatus.status[" на данном хосте.

    Returns:
        list[dict]: [{"name": "myvm", "status_nb": "active", "status_raw": "running"}, ...]
    """
    # Ищем все items с ключом vmstatus.status[*] на этом хосте
    items = zabbix_api.item.get(
        hostids=hostid,
        search={"key_": "vmstatus.status["},
        output=["key_", "lastvalue"]
    )

    if not items:
        print(f"    [!] Не найдено ни одного item vmstatus.status[*] на {node_name}")
        print(f"        Убедитесь что шаблон привязан и LLD discovery отработал")
        loging(f"[KVM] no vmstatus.status[] items found on {node_name}", "error")
        return []

    result = []
    for item in items:
        key    = item.get("key_", "")
        status = item.get("lastvalue", "").strip()

        # Извлекаем VMNAME из ключа вида "vmstatus.status[myvm]"
        m = re.match(r'^vmstatus\.status\[(.+)\]$', key)
        if not m:
            loging(f"[KVM] unexpected key format: {key}", "debug")
            continue

        vm_name = m.group(1).strip()
        if not vm_name:
            continue

        result.append({
            "name":       vm_name,   # это имя станет частью имени VM в NetBox
            "status_nb":  kvm_status_to_nb(status),
            "status_raw": status,
        })
        loging(f"[KVM] {node_name}: VM={vm_name} status={status}", "debug")

    loging(f"[KVM] {node_name}: found {len(result)} VMs via vmstatus.status[]", "sync")
    return result


def parse_kvm_vm_resources(hostid, node_name):
    """
    Читает CPU и память VM через мастер-item vmstatistic_cpu_mem.

    vmstatistic_cpu_mem — RAW TEXT item, обновляется каждые 30 минут.
    Структура JSON:
        {"data": [{"VMNAME": "myvm", "actual": 4294967296, "nrVirtCpu": 4,
                   "available": 2147483648, ...}, ...]}

    Поля:
      actual    — полный объём ОЗУ VM в байтах (Total RAM allocated to VM)
      nrVirtCpu — количество vCPU (если нет — пробуем "vcpus")
      Преобразуем actual bytes → MB для NetBox (поле memory в MB).

    Returns:
        dict: {vm_name: {"memory_mb": N, "vcpus": N}, ...}
    """
    raw = get_kvm_raw_item(hostid, "vmstatistic_cpu_mem")
    if not raw:
        loging(f"[KVM] vmstatistic_cpu_mem empty on {node_name}", "error")
        return {}

    result = {}
    for rec in raw.get("data", []):
        vm_name = rec.get("VMNAME", "").strip()
        if not vm_name:
            continue
        actual_bytes = int(rec.get("actual", 0) or 0)
        memory_mb    = actual_bytes // (1024 * 1024) if actual_bytes else 0
        vcpus        = int(rec.get("nrVirtCpu", rec.get("vcpus", 0)) or 0)
        result[vm_name] = {"memory_mb": memory_mb, "vcpus": vcpus}

    return result


def parse_kvm_vm_disks(hostid, node_name):
    """
    Читает диски VM через мастер-item vm_blk_discovery.

    vm_blk_discovery — RAW TEXT item, обновляется каждые 30 минут.
    Структура JSON:
        {"data": [
            {"VMNAME": "myvm", "Target": "vda",
             "Source": "/var/lib/libvirt/images/myvm.qcow2",
             "Device": "disk", "Type": "file"},
            ...
        ]}

    Target  — имя устройства внутри VM (vda, vdb, hda, ...)
    Source  — путь к образу на хосте или пул/том
    Device  — тип: "disk", "cdrom", "floppy" (cdrom/floppy пропускаем)
    Type    — тип бэкенда: "file", "block", "network"

    Размер диска читается через dependent item:
        disk.Capacity[VMNAME,TARGET] → байты → конвертируем в MB.

    Имя диска в NetBox: "target:source" (уникально для VM).

    Returns:
        dict: {vm_name: [{"path": "vda:/path/to/img", "size_mb": N}, ...], ...}
    """
    raw = get_kvm_raw_item(hostid, "vm_blk_discovery")
    if not raw:
        loging(f"[KVM] vm_blk_discovery empty on {node_name}", "error")
        return {}

    result = {}
    for rec in raw.get("data", []):
        vm_name = rec.get("VMNAME", "").strip()
        target  = rec.get("Target", "").strip()
        source  = rec.get("Source", "").strip()
        device  = rec.get("Device", "").strip()
        if not vm_name or not target:
            continue
        # Пропускаем не-диски
        if device.lower() in ("cdrom", "floppy"):
            continue

        # Размер через dependent item (может отсутствовать если LLD ещё не запустился)
        cap_key = f"disk.Capacity[{vm_name},{target}]"
        cap_val = get_kvm_dependent_value(hostid, cap_key)
        size_mb = 0
        if cap_val:
            try:
                size_mb = int(float(cap_val)) // (1024 * 1024)
            except Exception:
                size_mb = 0

        # Формируем путь: "target:source" или просто "target" если source пустой
        disk_path = f"{target}:{source}" if source else target

        if vm_name not in result:
            result[vm_name] = []
        result[vm_name].append({"path": disk_path, "size_mb": size_mb})

    return result


def parse_kvm_vm_interfaces(hostid, node_name):
    """
    Читает сетевые интерфейсы VM через мастер-item vmlist_network.

    vmlist_network — RAW TEXT item, обновляется каждые 30 минут.
    Структура JSON:
        {"data": [
            {"VMNAME": "myvm", "Interface": "vnet0",
             "MAC": "aa:bb:cc:dd:ee:ff", "Model": "virtio",
             "Source": "br0", "Type": "bridge"},
            ...
        ]}

    Interface — имя виртуального сетевого интерфейса на хосте (vnet0, vnet1, ...)
    MAC       — MAC-адрес интерфейса
    Model     — модель сетевой карты (virtio, e1000, rtl8139, ...)
    Source    — бридж/сеть, к которой подключён интерфейс
    Type      — тип подключения (bridge, network, ...)

    Interface == "-1" означает "нет сетевого интерфейса" — фильтруем
    (аналогично фильтру шаблона: conditions NOT_MATCHES_REGEX "-1").

    Returns:
        dict: {vm_name: [{"name": "vnet0", "mac": "aa:bb:...", "enabled": True}, ...], ...}
    """
    raw = get_kvm_raw_item(hostid, "vmlist_network")
    if not raw:
        loging(f"[KVM] vmlist_network empty on {node_name}", "error")
        return {}

    result = {}
    for rec in raw.get("data", []):
        vm_name    = rec.get("VMNAME", "").strip()
        iface_name = rec.get("Interface", "").strip()
        mac        = rec.get("MAC", "").strip()
        if not vm_name or not iface_name or iface_name in ("-1", "-"):
            continue

        if vm_name not in result:
            result[vm_name] = []
        result[vm_name].append({
            "name":    iface_name,
            "mac":     mac if mac and mac not in ("-", "") else None,
            "enabled": True,  # KVM не сообщает состояние link через этот item
        })

    return result


def sync_kvm_vm_disks(nb_vm, kvm_disks):
    """Синхронизирует virtual_disks KVM VM: нет в NB→создать, изменился→обновить, лишний→удалить."""
    kvm_paths = {d["path"] for d in kvm_disks}
    nb_disks  = {
        d.name: d
        for d in netbox_api.virtualization.virtual_disks.filter(virtual_machine_id=nb_vm.id)
    }

    print(f"      Дисков в KVM: {len(kvm_disks)}  в NetBox: {len(nb_disks)}")

    for disk in kvm_disks:
        size_mb    = disk.get("size_mb", 0)
        size_label = f"{size_mb // 1000}G" if size_mb >= 1000 else f"{size_mb}M"
        if disk["path"] not in nb_disks:
            try:
                netbox_api.virtualization.virtual_disks.create({
                    "virtual_machine": nb_vm.id,
                    "name": disk["path"][:200],
                    "size": size_mb,
                })
                print(f"      + disk {disk['path']} ({size_label})  → created")
                loging(f"[{nb_vm.name}] KVM disk created: {disk['path']}", "sync")
            except Exception as e:
                print(f"      ! disk {disk['path']}  → ERROR: {e}")
                loging(f"[{nb_vm.name}] KVM disk create error: {e}", "error")
        else:
            nb_disk = nb_disks[disk["path"]]
            if nb_disk.size != size_mb:
                try:
                    nb_disk.update({"size": size_mb})
                    print(f"      ~ disk {disk['path']} ({size_label})  → size updated")
                    loging(f"[{nb_vm.name}] KVM disk size updated: {disk['path']}", "sync")
                except Exception as e:
                    print(f"      ! disk {disk['path']}  → update ERROR: {e}")
                    loging(f"[{nb_vm.name}] KVM disk update error: {e}", "error")
            else:
                print(f"      = disk {disk['path']} ({size_label})  → ok")
                loging(f"[{nb_vm.name}] KVM disk skip (ok): {disk['path']}", "debug")

    for name, nb_disk in nb_disks.items():
        if name not in kvm_paths:
            try:
                nb_disk.delete()
                print(f"      - disk {name}  → deleted")
                loging(f"[{nb_vm.name}] KVM disk deleted: {name}", "sync")
            except Exception as e:
                print(f"      ! disk {name}  → delete ERROR: {e}")
                loging(f"[{nb_vm.name}] KVM disk delete error: {e}", "error")


def sync_kvm_vm_interfaces(nb_vm, kvm_ifaces, all_macs_cache):
    """Синхронизирует интерфейсы KVM VM: нет в NB→создать+MAC, изменился→обновить, лишний→удалить."""
    kvm_names = {i["name"] for i in kvm_ifaces}
    nb_ifaces  = {
        i.name: i
        for i in netbox_api.virtualization.interfaces.filter(virtual_machine_id=nb_vm.id)
    }

    for iface in kvm_ifaces:
        if iface["name"] not in nb_ifaces:
            try:
                nb_iface = netbox_api.virtualization.interfaces.create({
                    "virtual_machine": nb_vm.id,
                    "name":    iface["name"],
                    "enabled": iface.get("enabled", True),
                })
                loging(f"[{nb_vm.name}] KVM iface created: {iface['name']}", "sync")
                if iface.get("mac"):
                    _assign_mac(nb_iface, iface["mac"], all_macs_cache)
            except Exception as e:
                loging(f"[{nb_vm.name}] KVM iface create error: {e}", "error")
        else:
            nb_iface = nb_ifaces[iface["name"]]
            if nb_iface.enabled != iface.get("enabled", True):
                try:
                    nb_iface.update({"enabled": iface.get("enabled", True)})
                except Exception as e:
                    loging(f"[{nb_vm.name}] KVM iface update error: {e}", "error")
            if iface.get("mac"):
                _assign_mac(nb_iface, iface["mac"], all_macs_cache)

    for name, nb_iface in nb_ifaces.items():
        if name not in kvm_names:
            try:
                nb_iface.delete()
                loging(f"[{nb_vm.name}] KVM iface deleted: {name}", "sync")
            except Exception as e:
                loging(f"[{nb_vm.name}] KVM iface delete error: {e}", "error")


def sync_kvm_host(host_info, nb_cluster, role_vm_id, all_macs_cache, kvm_vm_names):
    """Синхронизирует все VM одного KVM-гипервизора с NetBox. Имя VM: node/VMNAME."""
    node_name = host_info["zabbix_name"].split(".")[0]
    hostid    = host_info["hostid"]

    print(f"  [>] KVM-гипервизор: {node_name} (hostid={hostid})")
    loging(f"[KVM] Processing host: {node_name}", "sync")

    # Шаг 1: получаем список VM
    vm_list = parse_kvm_vm_list(hostid, node_name)
    if not vm_list:
        return  # ошибка уже залогирована в parse_kvm_vm_list

    # Шаги 2-4: читаем все данные (по одному запросу на тип)
    resources  = parse_kvm_vm_resources(hostid, node_name)
    all_disks  = parse_kvm_vm_disks(hostid, node_name)
    all_ifaces = parse_kvm_vm_interfaces(hostid, node_name)

    print(f"    VM: {len(vm_list)},  с ресурсами: {len(resources)},  "
          f"с дисками: {len(all_disks)},  с интерфейсами: {len(all_ifaces)}")

    # Привязываем гипервизор к кластеру если найден в NetBox
    host_dev = nb_find_device(node_name)
    if host_dev and (not host_dev.cluster or host_dev.cluster.id != nb_cluster.id):
        host_dev.cluster = {"id": nb_cluster.id}
        try:
            host_dev.save()
            loging(f"[KVM] Node {node_name} → cluster {nb_cluster.name}", "sync")
        except Exception as e:
            loging(f"[KVM] Node bind error {node_name}: {e}", "error")

    # Шаг 5-6: обрабатываем каждую VM
    for vm in vm_list:
        vm_name    = vm["name"]
        vm_nb_name = f"{node_name}/{vm_name}"
        kvm_vm_names.add(vm_nb_name)

        nb_status  = vm["status_nb"]
        res        = resources.get(vm_name, {})
        memory_mb  = res.get("memory_mb", 0)
        vcpus      = res.get("vcpus", 0)
        kvm_disks  = all_disks.get(vm_name, [])
        kvm_ifaces = all_ifaces.get(vm_name, [])

        nb_vm = netbox_api.virtualization.virtual_machines.get(name=vm_nb_name)

        if nb_vm is None:
            # --- Создание VM ---
            create_data = {
                "name":    vm_nb_name,
                "cluster": nb_cluster.id,
                "status":  nb_status,
            }
            if vcpus:      create_data["vcpus"]  = vcpus
            if memory_mb:  create_data["memory"] = memory_mb
            if role_vm_id: create_data["role"]   = role_vm_id
            if host_dev:   create_data["device"] = host_dev.id
            if ZABBIX_TAG: create_data["tags"]   = [ZABBIX_TAG.id]

            try:
                nb_vm = netbox_api.virtualization.virtual_machines.create(create_data)
                loging(f"[{node_name}] KVM VM created: {vm_nb_name}", "sync")
                print(f"    + {vm_nb_name}  (status={vm['status_raw']}, "
                      f"vcpus={vcpus}, mem={memory_mb}MB)")
            except Exception as e:
                loging(f"[{node_name}] KVM VM create error {vm_nb_name}: {e}", "error")
                continue
        else:
            # --- Обновление VM (только изменившиеся поля) ---
            changed = False
            changed_fields = []

            if nb_vm.status and nb_vm.status.value != nb_status:
                nb_vm.status = nb_status; changed = True
                changed_fields.append(f"status→{nb_status}")
            else:
                loging(f"[{node_name}] KVM VM skip status (ok): {vm_nb_name}", "debug")

            if vcpus and nb_vm.vcpus != vcpus:
                nb_vm.vcpus = vcpus; changed = True
                changed_fields.append("vcpus")
            elif vcpus:
                loging(f"[{node_name}] KVM VM skip vcpus (ok): {vm_nb_name}", "debug")

            if memory_mb and nb_vm.memory != memory_mb:
                nb_vm.memory = memory_mb; changed = True
                changed_fields.append("memory")
            elif memory_mb:
                loging(f"[{node_name}] KVM VM skip memory (ok): {vm_nb_name}", "debug")

            if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                nb_vm.role = role_vm_id; changed = True
                changed_fields.append("role")

            if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                nb_vm.device = host_dev.id; changed = True
                changed_fields.append("device")

            # Тег zbb — добавляем если отсутствует
            current_tag_names = {t["name"] for t in (nb_vm.tags or [])}
            if ZABBIX_TAG and ZABBIX_TAG.name not in current_tag_names:
                nb_vm.tags = list(nb_vm.tags or []) + [ZABBIX_TAG.id]
                changed = True; changed_fields.append("tag+zbb")

            if changed:
                try:
                    nb_vm.save()
                    loging(f"[{node_name}] KVM VM updated ({', '.join(changed_fields)}): {vm_nb_name}", "sync")
                    print(f"    ~ {vm_nb_name}  [{', '.join(changed_fields)}]")
                except Exception as e:
                    loging(f"[{node_name}] KVM VM update error {vm_nb_name}: {e}", "error")
            else:
                print(f"    = {vm_nb_name}  → ok (no changes)")
                loging(f"[{node_name}] KVM VM skip (no changes): {vm_nb_name}", "debug")

        sync_kvm_vm_disks(nb_vm, kvm_disks)
        sync_kvm_vm_interfaces(nb_vm, kvm_ifaces, all_macs_cache)


def sync_kvm_cluster(kvm_hosts, cluster_name, role_vm_id):
    """Синхронизирует VM всех KVM-гипервизоров в один кластер NetBox."""
    loging(f"[KVM] Start sync cluster: {cluster_name}", "sync")
    print(f"\n[KVM] Кластер: {cluster_name}")

    nb_cluster = get_or_create_kvm_cluster(cluster_name)
    loging(f"[KVM] NetBox cluster: {cluster_name} id={nb_cluster.id}", "sync")

    # Один запрос для кэша MAC-адресов (используется _assign_mac)
    all_macs_cache = {str(m.mac_address) for m in netbox_api.dcim.mac_addresses.all()}
    kvm_vm_names   = set()  # накапливаем имена VM, которые видели на гипервизорах

    for host_info in kvm_hosts:
        sync_kvm_host(host_info, nb_cluster, role_vm_id, all_macs_cache, kvm_vm_names)

    # Удаляем из NetBox VM, которых нет ни на одном обработанном гипервизоре.
    # ВАЖНО: удаляем только VM с нод, которые мы обходили.
    # VM с необработанных гипервизоров не трогаем.
    processed_nodes = {h["zabbix_name"].split(".")[0] for h in kvm_hosts}
    for nb_vm in netbox_api.virtualization.virtual_machines.filter(cluster_id=nb_cluster.id):
        if nb_vm.name in kvm_vm_names:
            continue
        vm_node = nb_vm.name.split("/")[0] if "/" in nb_vm.name else ""
        if vm_node not in processed_nodes:
            continue  # принадлежит необработанному гипервизору — не трогаем
        try:
            nb_vm.delete()
            loging(f"[KVM] VM deleted: {nb_vm.name}", "sync")
            print(f"    - удалена: {nb_vm.name}")
        except Exception as e:
            loging(f"[KVM] VM delete error {nb_vm.name}: {e}", "error")

    loging(f"[KVM] Done: {cluster_name}", "sync")


# --- main() ---

def main():
    """Точка входа. Оркестрирует все режимы синхронизации."""

    # --- Шаг 1: проверка доступности NetBox ---
    print("\n[*] Проверяю подключение к NetBox (тег + роль)...")
    if not init_zabbix_resources():
        loging("Failed to initialize resources, exiting", "error")
        print("[!] Не удалось инициализировать ресурсы NetBox (тег/роль), см. error лог.")
        return
    print("[✓] NetBox: OK\n")

    # --- Шаг 2: выбор режима ---
    sync_devices, sync_disks_flag, sync_vms_flag, sync_kvm_flag = select_sync_mode()

    # --- Шаг 3a: выбор Zabbix-групп (для режимов устройства/диски) ---
    groups = []
    if sync_devices or sync_disks_flag:
        print("\n  [Шаг: выбор групп Zabbix для синхронизации устройств/дисков]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

    # --- Шаг 3b: выбор PVE-кластеров ---
    pve_clusters = []
    if sync_vms_flag:
        pve_template_id = cfg["pve_template_id"]
        if not pve_template_id:
            print("[!] Для синхронизации PVE VM нужна секция [PROXMOX] с template_id в config_disk.ini")
            print(CONFIG_EXAMPLE)
            return
        print("\n  [Шаг: выбор PVE-кластеров для синхронизации VM]")

        # Если группы уже выбраны — ограничиваем PVE-хосты только ими
        allowed_hostids = None
        if groups:
            allowed_hostids = {h["hostid"] for g in groups for h in g["hosts"]}

        pve_clusters = select_pve_clusters(pve_template_id, allowed_hostids=allowed_hostids)
        if not pve_clusters:
            print("[!] Кластеры PVE не выбраны, выход.")
            return

    # --- Шаг 3c: выбор KVM-гипервизоров ---
    kvm_hosts = []
    if sync_kvm_flag:
        kvm_template_id = cfg["kvm_template_id"]
        if not kvm_template_id:
            print("[!] Для синхронизации KVM VM нужна секция [KVM] с template_id в config_disk.ini")
            print(CONFIG_EXAMPLE)
            return
        print("\n  [Шаг: выбор KVM-гипервизоров для синхронизации VM]")

        allowed_hostids = None
        if groups:
            allowed_hostids = {h["hostid"] for g in groups for h in g["hosts"]}

        kvm_hosts = select_kvm_hosts(kvm_template_id, allowed_hostids=allowed_hostids)
        if not kvm_hosts:
            print("[!] KVM-хосты не выбраны, выход.")
            return

    # --- Шаг 4: подтверждение запуска ---
    mode_label = []
    if sync_devices:    mode_label.append("устройства")
    if sync_disks_flag: mode_label.append("диски")
    if sync_vms_flag:   mode_label.append("PVE VM")
    if sync_kvm_flag:   mode_label.append("KVM VM")

    print(f"\n{'=' * 50}")
    print(f"  Режим:  {' + '.join(mode_label)}")
    if groups:
        print(f"  Групп:  {len(groups)}  ({sum(len(g['hosts']) for g in groups)} хостов)")
    if pve_clusters:
        print(f"  PVE-кластеров: {len(pve_clusters)}")
    if kvm_hosts:
        print(f"  KVM-гипервизоров: {len(kvm_hosts)}")
    print(f"{'=' * 50}")
    confirm = input("Запустить синхронизацию? [y/n]: ").strip().lower()
    if confirm != "y":
        print("Отменено.")
        return

    loging("=" * 50, "sync")
    loging(f"Start sync | mode: {'+'.join(mode_label)}", "sync")

    # --- Шаг 5a: устройства и/или диски ---
    if sync_devices or sync_disks_flag:
        # Шаблоны, для хостов с которыми запускаем синхронизацию
        SYNC_TEMPLATES = ("Linux by Zabbix agent", "Proxmox VE by HTTP")

        for group in groups:
            loging(f"Processing group: {group['groupname']}", "sync")
            print(f"\n[>] Группа: {group['groupname']} ({len(group['hosts'])} хостов)")

            for host in group["hosts"]:
                hostid    = host["hostid"]
                templates = get_host_templates(hostid)

                # Пропускаем хосты без нужных шаблонов
                if not any(t in templates for t in SYNC_TEMPLATES):
                    continue

                loging(f"Processing host: {host['name']}", "sync")
                print(f"    - {host['name']}")
                update_netbox_device(
                    hostid,
                    sync_devices=sync_devices,
                    sync_disks_flag=sync_disks_flag
                )

    # --- Шаг 5b: PVE VM ---
    if sync_vms_flag:
        # Группируем выбранные ноды по кластеру (общий host:port = один кластер)
        processed_clusters = {}
        for h in pve_clusters:
            key = f"{h['host']}:{h['port']}"
            if key not in processed_clusters:
                processed_clusters[key] = {"entry": h, "nodes": set()}
            processed_clusters[key]["nodes"].add(h["zabbix_name"].split(".")[0])

        for key, cluster_data in processed_clusters.items():
            sync_pve_cluster(cluster_data["entry"], allowed_nodes=cluster_data["nodes"])

    # --- Шаг 5c: KVM VM ---
    if sync_kvm_flag:
        sync_kvm_cluster(
            kvm_hosts    = kvm_hosts,
            cluster_name = cfg["kvm_cluster"],
            role_vm_id   = cfg.get("kvm_role_vm"),
        )

    loging("Sync finished", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация завершена.")


# --- Точка входа ---

if __name__ == "__main__":
    main()
