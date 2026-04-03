#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# common.py — общие утилиты, конфиг, инициализация API
# Импортируется всеми скриптами синхронизации.

import os
import re
import sys
import datetime
import time
import configparser

import urllib3
import pynetbox
from zabbix_utils import ZabbixAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# --- Конфигурация ---

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.ini")

CONFIG_EXAMPLE = """\
Пример config.ini:

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
    }


# --- Инициализация API ---

cfg = load_config()

zabbix_api = ZabbixAPI(cfg["zabbix_url"])
zabbix_api.login(cfg["zabbix_token"])

netbox_api = pynetbox.api(cfg["netbox_url"], cfg["netbox_token"])
netbox_api.http_session.verify = False


# --- Утилиты ---

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
            line = "\\" + line
        if line.startswith(("-", "*", "+")):
            line = "\\" + line
        result.append(line)
    return "\n".join(result)


def loging(data="", namefile="sync"):
    """Записывает строку в лог-файл с временной меткой (sync/error/debug)."""
    date     = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.datetime.now().strftime("%H:%M:%S")
    filename = f"{namefile}_{date}.log"
    with open(filename, "a+", encoding="utf-8") as f:
        f.write(f"{time_str} :: {data}\n")


# --- ZBX-блок в comments устройств ---

ZBX_BLOCK_MARKER = "== zabbix description =="


def build_zbx_block(text):
    if not text:
        return ""
    return f"{ZBX_BLOCK_MARKER}\n\n{text}\n\n{ZBX_BLOCK_MARKER}"


def inject_zbx_block(current_comments, new_text):
    """Вставляет или обновляет ZBX-блок в comments. Текст вне блока не трогает."""
    pattern = re.compile(
        rf"{re.escape(ZBX_BLOCK_MARKER)}\n.*?\n{re.escape(ZBX_BLOCK_MARKER)}",
        re.DOTALL
    )
    new_block = build_zbx_block(new_text) if new_text else ""

    if pattern.search(current_comments):
        result = pattern.sub(new_block, current_comments) if new_block else pattern.sub("", current_comments)
        return result.strip()

    if new_block:
        base = current_comments.strip()
        return (base + "\n\n" + new_block).strip() if base else new_block
    return current_comments


def extract_zbx_block_text(comments):
    """Извлекает содержимое ZBX-блока для сравнения."""
    pattern = re.compile(
        rf"{re.escape(ZBX_BLOCK_MARKER)}\n(.*?)\n{re.escape(ZBX_BLOCK_MARKER)}",
        re.DOTALL
    )
    m = pattern.search(comments or "")
    return m.group(1).strip() if m else ""


# --- NetBox: get-or-create, retry ---

ZABBIX_TAG = None   # тег "zbb"
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
                raise
    raise last_exc


def get_or_create_tag(name, color="green"):
    tag = netbox_call_with_retry(lambda: netbox_api.extras.tags.get(name=name))
    if tag:
        return tag
    try:
        tag = netbox_api.extras.tags.create(name=name, slug=slugify(name), color=color)
        loging(f"[TAG] Created tag: {name}", "sync")
        return tag
    except Exception as e:
        loging(f"[TAG CREATE ERROR] {e}", "error")
        return netbox_call_with_retry(lambda: netbox_api.extras.tags.get(name=name))


def get_or_create_inventory_role(name, slug=None):
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


def init_resources():
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


def nb_find_device(name):
    """Ищет device в NetBox по короткому имени, затем с доменом из конфига."""
    device = netbox_api.dcim.devices.get(name=name)
    if device:
        return device
    domain = cfg.get("pve_domain", "")
    if domain:
        device = netbox_api.dcim.devices.get(name=f"{name}{domain}")
    return device


# --- Интерактив: выбор групп ---

import fnmatch


def apply_glob_patterns(all_groups, patterns):
    """Фильтрует список групп по glob-паттернам."""
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
    """Интерактивный выбор групп Zabbix (glob-фильтр + номера)."""
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


def select_missing_vm_behavior():
    """Спрашивает что делать с VM исчезнувшими с гипервизора: удалить или offline."""
    print("\n" + "=" * 50)
    print("  Поведение при исчезнувших VM")
    print("  (VM есть в NetBox, но не найдена на гипервизоре)")
    print("=" * 50)
    print("  y — Удалить из NetBox")
    print("  n — Оставить, перевести в статус offline (для истории)")
    print("=" * 50)

    while True:
        choice = input("  Удалять исчезнувшие VM? [y/n]: ").strip().lower()
        if choice == "y":
            print("  → Исчезнувшие VM будут удалены из NetBox")
            loging("Missing VM behavior: delete", "sync")
            return "delete"
        if choice == "n":
            print("  → Исчезнувшие VM будут переведены в offline, тег zbb будет убран")
            loging("Missing VM behavior: offline", "sync")
            return "offline"
        print("  [!] Введите y или n")


def _handle_missing_vm(nb_vm, missing_vm_behavior):
    """Удаляет или переводит в offline VM отсутствующую на гипервизоре.
    При offline — также убирает тег zbb.
    """
    if missing_vm_behavior == "delete":
        try:
            nb_vm.delete()
            loging(f"[VM] Deleted missing VM: {nb_vm.name}", "sync")
            print(f"    - удалена: {nb_vm.name}")
        except Exception as e:
            loging(f"[VM] Delete error {nb_vm.name}: {e}", "error")
            print(f"    ! ошибка удаления {nb_vm.name}: {e}")
    else:
        try:
            current_status = nb_vm.status.value if nb_vm.status else ""
            update_data    = {}
            changed_fields = []

            if current_status != "offline":
                update_data["status"] = "offline"
                changed_fields.append("status→offline")

            # убираем тег zbb
            current_tag_ids = [t.id for t in (nb_vm.tags or [])]
            if ZABBIX_TAG and ZABBIX_TAG.id in current_tag_ids:
                update_data["tags"] = [tid for tid in current_tag_ids if tid != ZABBIX_TAG.id]
                changed_fields.append("tag-zbb")

            if update_data:
                nb_vm.update(update_data)
                loging(f"[VM] Missing VM updated ({', '.join(changed_fields)}): {nb_vm.name}", "sync")
                print(f"    ~ {nb_vm.name}  [{', '.join(changed_fields)}]")
            else:
                loging(f"[VM] Already offline, no zbb tag (skip): {nb_vm.name}", "debug")
        except Exception as e:
            loging(f"[VM] Set offline error {nb_vm.name}: {e}", "error")
            print(f"    ! ошибка offline {nb_vm.name}: {e}")
