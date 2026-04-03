#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# sync_network.py — синхронизация сетевых устройств (serial, interfaces, descriptions)
# Запускается напрямую или через main.py
#
# Обрабатывает сетевые устройства из групп Zabbix (Net/*, Net/Spine, Net/Leaf и т.д.)
# Вендоры: Juniper, Cisco, Eltex, Huawei, UserGate и другие.
#
# Что синхронизируется:
#   - serial number (из items / inventory)
#   - platform (модель → NetBox platform)
#   - device_type + manufacturer (при создании устройства)
#   - device_role (из имени группы Zabbix)
#   - site (из тега "site" в Zabbix)
#   - tags (zbb) + comments (ZBX-блок)
#   - интерфейсы: создание, обновление description, disabled

import re

from common import (
    cfg, zabbix_api, netbox_api,
    ZABBIX_TAG,
    loging, slugify, compact_text,
    inject_zbx_block, extract_zbx_block_text,
    get_or_create_tag, get_or_create_platform,
    nb_find_device,
    select_groups, init_resources,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Нормализация имён интерфейсов
# ═══════════════════════════════════════════════════════════════════════════════

# Таблица: полное/короткое имя → каноническая форма
# Порядок важен: длинные паттерны первыми (чтобы TenGigabitEthernet не захватился GigabitEthernet)
IFACE_NORMALIZE_MAP = [
    # Полные формы (Cisco SNMP, Huawei)
    (r"(?i)^HundredGig(?:abit)?E(?:thernet)?",  "hu"),
    (r"(?i)^FortyGig(?:abit)?E(?:thernet)?",     "fo"),
    (r"(?i)^TwentyFiveGig(?:abit)?E(?:thernet)?","tf"),
    (r"(?i)^TenGig(?:abit)?E(?:thernet)?",       "te"),
    (r"(?i)^TenGi",                               "te"),
    (r"(?i)^GigabitEthernet",                     "gi"),
    (r"(?i)^FastEthernet",                        "fa"),
    (r"(?i)^Ethernet",                            "eth"),
    # Короткие формы (NetBox / конфиги)
    (r"(?i)^ce-",    "hu"),    # Juniper 100G
    (r"(?i)^et-",    "fo"),    # Juniper 40G
    (r"(?i)^xe-",    "te"),    # Juniper 10G
    (r"(?i)^ge-",    "gi"),    # Juniper 1G
    (r"(?i)^fe-",    "fa"),    # Juniper 100M
    (r"(?i)^Hu(?=\d|/)",      "hu"),    # Huawei HundredGigE short
    (r"(?i)^XGE",             "te"),    # Huawei 10G
    (r"(?i)^GE(?=\d|/)",      "gi"),    # Huawei GigabitEthernet short
    (r"(?i)^Te(?=\d|/)",      "te"),    # Cisco short
    (r"(?i)^Gi(?=\d|/)",      "gi"),    # Cisco short
    (r"(?i)^Fa(?=\d|/)",      "fa"),    # Cisco short
    (r"(?i)^Eth(?=\d|/)",     "eth"),   # Generic short
]

# Имена интерфейсов, которые отсекаем (нефизические)
IFACE_SKIP_PATTERNS = [
    r"(?i)^Vlan",
    r"(?i)^Loopback",
    r"(?i)^Null",
    r"(?i)^mgmt",
    r"(?i)^lo\d*$",
    r"(?i)^irb",
    r"(?i)^vme",
    r"(?i)^jsrv",
    r"(?i)^fxp",
    r"(?i)^em\d",
    r"(?i)^ae\d",        # Juniper aggregate
    r"(?i)^Po\d",        # Cisco port-channel
    r"(?i)^Tunnel",
    r"(?i)^Dialer",
    r"(?i)^BVI",
    r"(?i)^Stack",
    r"(?i)^cpu",
    r"(?i)^Vlanif",
    r"(?i)^LoopBack",
    r"(?i)^NULL",
    r"(?i)^InLoopBack",
    r"(?i)^Register-Tunnel",
]

# Каноническая форма → тип интерфейса NetBox
CANONICAL_TO_NB_TYPE = {
    "fa":  "100base-tx",
    "gi":  "1000base-t",
    "te":  "10gbase-x-sfpp",
    "tf":  "25gbase-x-sfp28",
    "fo":  "40gbase-x-qsfpp",
    "hu":  "100gbase-x-qsfp28",
    "eth": "1000base-t",
}

# Известные вендоры для авто-определения manufacturer
KNOWN_VENDORS = [
    "Juniper", "Cisco", "Eltex", "Huawei", "UserGate",
    "Arista", "MikroTik", "Fortinet", "Mellanox", "Dell",
    "HP", "HPE", "Aruba", "ZTE", "Extreme", "Brocade",
]


def _safe_tag_ids(tags_list):
    """
    Извлекает ID тегов из списка, который может содержать
    как объекты pynetbox (с .id), так и голые int.
    Возвращает list[int].
    """
    ids = []
    for t in (tags_list or []):
        if isinstance(t, int):
            ids.append(t)
        elif hasattr(t, 'id'):
            ids.append(t.id)
    return ids


def _safe_tag_list_for_update(tags_list):
    """
    Приводит список тегов к формату [int, ...] для update().
    Элементы могут быть int или объектами pynetbox.
    """
    return _safe_tag_ids(tags_list)


def normalize_iface_name(name):
    """
    Приводит имя интерфейса к каноническому виду для матчинга.
    Возвращает (canonical, числовая_часть) или None если не удалось распознать.
    Пример: 'GigabitEthernet0/0/1' → ('gi', '0/0/1')
            'ge-0/0/1'             → ('gi', '0/0/1')
            'Gi0/1'                → ('gi', '0/1')
            'xe-0/0/58:3'          → ('te', '0/0/58:3')
    """
    name = name.strip()
    for pattern, canonical in IFACE_NORMALIZE_MAP:
        m = re.match(pattern, name)
        if m:
            rest = name[m.end():]
            # Убираем ведущий разделитель (- или /)
            rest = re.sub(r'^[-/]', '', rest)
            return (canonical, rest)
    return None


def is_physical_iface(iface_name, if_type=None):
    """Проверяет, является ли интерфейс физическим."""
    # По ifType: 6 = ethernetCsmacd
    if if_type is not None:
        try:
            if int(if_type) != 6:
                return False
        except (ValueError, TypeError):
            pass

    # Субинтерфейсы (unit): xe-0/0/58:3.918 — отсекаем
    # Точка после цифры/двоеточия = логический sub-interface
    if re.search(r'\.\d+$', iface_name):
        return False

    # По имени — отсекаем нефизические
    for pattern in IFACE_SKIP_PATTERNS:
        if re.match(pattern, iface_name):
            return False

    return True


def guess_nb_iface_type(iface_name):
    """Определяет тип интерфейса NetBox по имени."""
    norm = normalize_iface_name(iface_name)
    if norm:
        canonical, _ = norm
        return CANONICAL_TO_NB_TYPE.get(canonical, "other")
    return "other"


# ═══════════════════════════════════════════════════════════════════════════════
# Zabbix: данные хоста
# ═══════════════════════════════════════════════════════════════════════════════

def get_item_value(hostid, key_pattern, default=None):
    """Возвращает lastvalue item по частичному ключу."""
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


def get_net_host_data(hostid):
    """
    Собирает данные сетевого хоста из Zabbix.
    Возвращает dict: hostname, serial, serial_source, model, description.
    """
    host = zabbix_api.host.get(
        hostids=hostid,
        selectInventory="extend",
        selectTags="extend",
        selectGroups="extend",
        output=["hostid", "name", "host", "description"]
    )[0]

    inventory = host.get("inventory") or {}
    tags_raw  = host.get("tags", [])
    groups    = host.get("groups", []) or host.get("hostgroups", [])

    # --- Serial ---
    serial = None
    serial_source = None

    # Цепочка items: system.hw.serialnumber → huawei.serial → system.serialnumber
    serial_from_snmp = get_item_value(hostid, "system.hw.serialnumber")
    if serial_from_snmp:
        serial = serial_from_snmp
        serial_source = "item:system.hw.serialnumber"
    else:
        serial_huawei = get_item_value(hostid, "huawei.serial")
        if serial_huawei:
            serial = serial_huawei
            serial_source = "item:huawei.serial"
        else:
            serial_from_snmp2 = get_item_value(hostid, "system.serialnumber")
            if serial_from_snmp2:
                serial = serial_from_snmp2
                serial_source = "item:system.serialnumber"

    # Fallback: inventory (все поля где может быть серийник)
    if not serial:
        for inv_field in ("serialno_a", "serialno_b"):
            inv_val = inventory.get(inv_field, "").strip()
            if inv_val and inv_val.lower() not in ("0", "none", "null", "unknown", "n/a", ""):
                serial = inv_val
                serial_source = f"inventory:{inv_field}"
                break

    # --- Model / Platform ---
    model = get_item_value(hostid, "system.hw.model")
    if not model:
        model = inventory.get("model", "").strip() or None
    if not model:
        model = inventory.get("hardware_full", "").strip() or None
    if not model:
        model = inventory.get("system", "").strip() or None

    # --- Site из тега ---
    site_tag_value = None
    for tag in tags_raw:
        if tag.get("tag", "").lower() == "site":
            site_tag_value = tag.get("value", "").strip()
            break

    # --- Role из группы ---
    group_names = [g.get("name", "") for g in groups]

    return {
        "hostname":       host["name"],
        "host_technical": host.get("host", host["name"]),
        "serial":         serial,
        "serial_source":  serial_source,
        "model":          model,
        "description":    host.get("description", "").strip(),
        "site_tag":       site_tag_value,
        "group_names":    group_names,
        "inventory":      inventory,
    }


def _parse_iface_name_alias_from_item_name(item_name):
    """
    Извлекает имя интерфейса и alias из имени item в Zabbix.

    Форматы item name (из шаблонов):
      Cisco/Juniper/Huawei/Eltex:
        "Interface {#IFNAME}({#IFALIAS}): Operational status"
        "Interface {#IFNAME}({#IFALIAS}): Bits received"
      UserGate:
        "Operational status of interface {#SNMPVALUE}"

    Alias может содержать скобки, поэтому ищем ПОСЛЕДНЕЕ вхождение "): "
    как разделитель между NAME(ALIAS) и типом metric.

    Возвращает (iface_name, alias) или (None, None) при неудаче.
    """
    # Формат 1: "Interface NAME(ALIAS): metric_type"
    # Ищем последнее "): " — это конец блока NAME(ALIAS)
    if item_name.startswith("Interface "):
        rest = item_name[len("Interface "):]

        # Ищем последнее "): " в строке
        idx = rest.rfind("): ")
        if idx != -1:
            name_alias_part = rest[:idx + 1]  # включая закрывающую )
            # Теперь разбиваем name_alias_part на NAME и (ALIAS)
            # Ищем ПЕРВУЮ ( — всё до неё = имя, внутри = alias
            paren_idx = name_alias_part.find("(")
            if paren_idx != -1:
                iface_name = name_alias_part[:paren_idx].strip()
                alias = name_alias_part[paren_idx + 1:-1].strip()  # убираем ( и )
                if iface_name:
                    return iface_name, alias

        # Формат 1b: "Interface NAME: metric_type" (без alias)
        idx = rest.find(": ")
        if idx != -1:
            iface_name = rest[:idx].strip()
            if iface_name:
                return iface_name, ""

    # Формат 2 (UserGate): "... of interface NAME"
    m = re.search(r'of interface\s+(.+?)$', item_name)
    if m:
        return m.group(1).strip(), ""

    # Формат 3 (UserGate): "... on interface NAME"
    m = re.search(r'on interface\s+(.+?)$', item_name)
    if m:
        return m.group(1).strip(), ""

    return None, None


def _extract_snmpindex_from_key(key):
    """
    Извлекает SNMPINDEX из ключа item.

    Примеры:
      net.if.status[ifOperStatus.42]    → "42"
      net.if.type[ifType.42]            → "42"
      net.if.speed[ifHighSpeed.42]      → "42"
      net.if.in[ifHCInOctets.42]        → "42"
      ifOperStatus[eth0]                → "eth0"  (UserGate)
    """
    m = re.search(r'\[.*?\.(\d+)\]$', key)
    if m:
        return m.group(1)
    # UserGate: ifOperStatus[interface_name]
    m = re.search(r'\[(.+?)\]$', key)
    if m:
        return m.group(1)
    return None


def get_net_interfaces_from_zabbix(hostid):
    """
    Получает физические интерфейсы сетевого устройства из Zabbix.

    Стратегия:
    1. Ищем items с ключами net.if.status[ifOperStatus.*] — это есть у всех
       вендоров (Cisco, Juniper, Huawei, Eltex). Один item = один интерфейс.
    2. Имя интерфейса и alias парсим из поля name этого item:
       "Interface GigabitEthernet0/0/1(uplink-to-core): Operational status"
    3. Тип и скорость берём из соседних items по тому же SNMPINDEX.
    4. Для UserGate ищем ifOperStatus[*] — другой формат ключей.

    Возвращает список dict: {name, alias, if_type, speed, oper_status, if_index}
    """
    # --- Шаг 1: собираем ВСЕ items с net.if. и ifOperStatus/ifDescr ---
    items_netif = zabbix_api.item.get(
        hostids=hostid,
        search={"key_": "net.if."},
        output=["key_", "lastvalue", "name"]
    )
    items_usergate = zabbix_api.item.get(
        hostids=hostid,
        search={"key_": "ifOperStatus"},
        output=["key_", "lastvalue", "name"]
    )

    all_items = items_netif + items_usergate

    if not all_items:
        loging(f"[hostid={hostid}] No net.if.* or ifOperStatus items found", "debug")
        return []

    # --- Шаг 2: находим "опорные" items (status) — по ним определяем список интерфейсов ---
    # Ключи-якоря: net.if.status[ifOperStatus.INDEX] или ifOperStatus[NAME]
    status_items = []
    for item in all_items:
        key = item["key_"]
        if re.match(r'net\.if\.status\[ifOperStatus\.\d+\]', key):
            status_items.append(item)
        elif re.match(r'ifOperStatus\[.+\]', key) and not key.startswith("net."):
            status_items.append(item)

    if not status_items:
        loging(f"[hostid={hostid}] No status items found (net.if.status/ifOperStatus)", "debug")
        return []

    # --- Шаг 3: для каждого status item извлекаем имя, alias, snmpindex ---
    iface_list = {}  # snmpindex → {name, alias, oper_status}
    for item in status_items:
        iface_name, alias = _parse_iface_name_alias_from_item_name(item["name"])
        snmpindex = _extract_snmpindex_from_key(item["key_"])

        if not iface_name or not snmpindex:
            loging(f"[hostid={hostid}] Cannot parse status item: key={item['key_']} name={item['name']}", "debug")
            continue

        iface_list[snmpindex] = {
            "name":        iface_name,
            "alias":       alias,
            "oper_status": item.get("lastvalue", "").strip(),
            "if_type":     None,
            "speed":       None,
        }

    # --- Шаг 4: дополняем type и speed из соседних items ---
    for item in all_items:
        key = item["key_"]

        # net.if.type[ifType.INDEX]
        m = re.match(r'net\.if\.type\[ifType\.(\d+)\]', key)
        if m and m.group(1) in iface_list:
            iface_list[m.group(1)]["if_type"] = item.get("lastvalue", "").strip()
            continue

        # net.if.speed[ifHighSpeed.INDEX]
        m = re.match(r'net\.if\.speed\[ifHighSpeed\.(\d+)\]', key)
        if m and m.group(1) in iface_list:
            iface_list[m.group(1)]["speed"] = item.get("lastvalue", "").strip()
            continue

    # --- Шаг 5: дополняем alias из items с traffic/description по тому же snmpindex ---
    # Если alias пустой, пробуем найти его в других items этого же интерфейса
    for item in all_items:
        key = item["key_"]
        item_name = item["name"]

        # Пропускаем status items — их уже обработали
        if "ifOperStatus" in key:
            continue

        snmpindex = _extract_snmpindex_from_key(key)
        if not snmpindex or snmpindex not in iface_list:
            continue

        # Если alias ещё пустой — пробуем извлечь из другого item
        if not iface_list[snmpindex]["alias"]:
            _, alias = _parse_iface_name_alias_from_item_name(item_name)
            if alias:
                iface_list[snmpindex]["alias"] = alias

    # --- Шаг 6: фильтруем физические ---
    result = []
    for snmpindex, data in iface_list.items():
        if not is_physical_iface(data["name"], data["if_type"]):
            continue

        result.append({
            "name":        data["name"],
            "alias":       data["alias"] or "",
            "if_type":     data["if_type"],
            "speed":       data["speed"] or "",
            "oper_status": data["oper_status"],
            "if_index":    snmpindex,
        })

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# NetBox: get-or-create для сетевых объектов
# ═══════════════════════════════════════════════════════════════════════════════

def get_or_create_manufacturer(name):
    """Получает или создаёт manufacturer в NetBox."""
    if not name:
        return None
    slug = slugify(name)
    mfr = netbox_api.dcim.manufacturers.get(slug=slug)
    if mfr:
        return mfr
    mfr = netbox_api.dcim.manufacturers.get(name=name)
    if mfr:
        return mfr
    try:
        mfr = netbox_api.dcim.manufacturers.create(name=name, slug=slug)
        loging(f"[MANUFACTURER] Created: {name}", "sync")
        return mfr
    except Exception as e:
        loging(f"[MANUFACTURER CREATE ERROR] {e}", "error")
        return netbox_api.dcim.manufacturers.get(name=name) or netbox_api.dcim.manufacturers.get(slug=slug)


def get_or_create_device_type(model_name, manufacturer_name=None):
    """Получает или создаёт device_type в NetBox."""
    if not model_name:
        return None

    # Ищем по модели
    dt = netbox_api.dcim.device_types.get(model=model_name)
    if dt:
        return dt

    # Определяем manufacturer
    mfr_name = manufacturer_name or guess_manufacturer(model_name)
    mfr = get_or_create_manufacturer(mfr_name) if mfr_name else None

    if not mfr:
        # Пробуем создать "Unknown" manufacturer
        mfr = get_or_create_manufacturer("Unknown")
        if not mfr:
            loging(f"[DEVICE TYPE] Cannot create without manufacturer: {model_name}", "error")
            return None

    try:
        dt = netbox_api.dcim.device_types.create(
            model=model_name[:100],
            slug=slugify(model_name),
            manufacturer=mfr.id,
        )
        loging(f"[DEVICE TYPE] Created: {model_name} (mfr={mfr.name})", "sync")
        return dt
    except Exception as e:
        loging(f"[DEVICE TYPE CREATE ERROR] {model_name}: {e}", "error")
        return netbox_api.dcim.device_types.get(model=model_name)


def get_or_create_device_role(name):
    """Получает или создаёт device role в NetBox."""
    if not name:
        return None
    slug = slugify(name)
    # Пробуем найти по slug
    role = netbox_api.dcim.device_roles.get(slug=slug)
    if role:
        return role
    # Пробуем найти по name (slug может не совпадать)
    role = netbox_api.dcim.device_roles.get(name=name)
    if role:
        return role
    try:
        role = netbox_api.dcim.device_roles.create(name=name, slug=slug, color="2196f3")
        loging(f"[DEVICE ROLE] Created: {name}", "sync")
        return role
    except Exception as e:
        loging(f"[DEVICE ROLE CREATE ERROR] {e}", "error")
        # Race condition или slug-коллизия — пробуем ещё раз найти
        role = netbox_api.dcim.device_roles.get(name=name)
        if role:
            return role
        return netbox_api.dcim.device_roles.get(slug=slug)


def find_site_by_tag(site_tag_value):
    """Ищет site в NetBox по значению тега (name или slug)."""
    if not site_tag_value:
        return None
    site = netbox_api.dcim.sites.get(name=site_tag_value)
    if site:
        return site
    site = netbox_api.dcim.sites.get(slug=slugify(site_tag_value))
    return site


def guess_manufacturer(model_name):
    """Пытается определить manufacturer из названия модели."""
    if not model_name:
        return None
    model_lower = model_name.lower()
    for vendor in KNOWN_VENDORS:
        if vendor.lower() in model_lower:
            return vendor
    # Пробуем первое слово
    first_word = model_name.split()[0] if model_name.split() else None
    if first_word:
        for vendor in KNOWN_VENDORS:
            if first_word.lower() == vendor.lower():
                return vendor
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Синхронизация интерфейсов
# ═══════════════════════════════════════════════════════════════════════════════

def sync_interfaces(device, zbx_ifaces, device_name):
    """
    Синхронизирует физические интерфейсы устройства: Zabbix → NetBox.

    - Есть в Zabbix, нет в NetBox      → создаём + description + тег zbb
    - Есть в обоих                      → обновляем description + тег zbb
    - Есть в NetBox (с тегом zbb), нет в Zabbix → disabled
    """
    from common import ZABBIX_TAG

    if not zbx_ifaces:
        loging(f"[{device_name}] No physical interfaces from Zabbix — skip iface sync", "debug")
        print(f"      Интерфейсов в Zabbix: 0 — пропуск")
        return

    # Получаем интерфейсы из NetBox
    nb_ifaces_raw = list(netbox_api.dcim.interfaces.filter(device_id=device.id))
    nb_ifaces_by_name = {i.name: i for i in nb_ifaces_raw}

    # Строим нормализованный индекс NetBox: canonical_key → nb_iface
    nb_normalized = {}
    for nb_iface in nb_ifaces_raw:
        norm = normalize_iface_name(nb_iface.name)
        if norm:
            key = f"{norm[0]}:{norm[1]}"
            nb_normalized[key] = nb_iface

    matched_nb_ids = set()  # ID интерфейсов NetBox, которые сматчились с Zabbix
    created = 0
    updated = 0
    skipped = 0

    print(f"      Интерфейсов в Zabbix: {len(zbx_ifaces)}  в NetBox: {len(nb_ifaces_raw)}")

    for zbx_if in zbx_ifaces:
        zbx_name  = zbx_if["name"]
        zbx_alias = zbx_if["alias"]

        # 1. Точное совпадение имени
        nb_iface = nb_ifaces_by_name.get(zbx_name)

        # 2. Нормализованный матчинг
        if not nb_iface:
            norm = normalize_iface_name(zbx_name)
            if norm:
                key = f"{norm[0]}:{norm[1]}"
                nb_iface = nb_normalized.get(key)
                # 3. Cisco stack fallback: Gi0/48 ↔ Gi1/0/48
                #    Если числовая часть X/Y — пробуем */X/Y (добавляем member)
                #    Если числовая часть A/X/Y — пробуем X/Y (убираем member)
                if not nb_iface:
                    parts = norm[1].split("/")
                    if len(parts) == 2:
                        # 0/48 → пробуем */0/48
                        for candidate_key, candidate_iface in nb_normalized.items():
                            if candidate_key.startswith(norm[0] + ":") and candidate_key.endswith("/" + norm[1]):
                                nb_iface = candidate_iface
                                break
                    elif len(parts) == 3:
                        # 1/0/48 → пробуем 0/48 (убираем первый элемент)
                        short_rest = "/".join(parts[1:])
                        short_key = f"{norm[0]}:{short_rest}"
                        nb_iface = nb_normalized.get(short_key)
                if nb_iface:
                    loging(f"[{device_name}] Iface normalized match: zbx='{zbx_name}' → nb='{nb_iface.name}'", "debug")
            else:
                loging(f"[{device_name}] Iface normalize failed: '{zbx_name}'", "debug")

        if nb_iface:
            # --- Найден → обновляем ---
            matched_nb_ids.add(nb_iface.id)
            update_data = {}
            changed_fields = []

            # Description
            current_descr = (nb_iface.description or "").strip()
            if zbx_alias and current_descr != zbx_alias:
                update_data["description"] = zbx_alias
                changed_fields.append(f"descr: '{current_descr[:30]}' → '{zbx_alias[:30]}'")

            # Тег zbb
            current_tags = list(nb_iface.tags) if nb_iface.tags else []
            current_tag_ids = _safe_tag_ids(current_tags)
            if ZABBIX_TAG and ZABBIX_TAG.id not in current_tag_ids:
                new_tag_list = _safe_tag_list_for_update(current_tags)
                new_tag_list.append(ZABBIX_TAG.id)
                update_data["tags"] = new_tag_list
                changed_fields.append("tag+zbb")

            # Enabled (убеждаемся что включён, раз пришёл из Zabbix)
            if not nb_iface.enabled:
                update_data["enabled"] = True
                changed_fields.append("enabled→true")

            if update_data:
                try:
                    nb_iface.update(update_data)
                    print(f"      ~ iface {nb_iface.name}  [{', '.join(changed_fields)}]  → updated")
                    loging(f"[{device_name}] Iface updated: {nb_iface.name} ({', '.join(changed_fields)})", "sync")
                    updated += 1
                except Exception as e:
                    print(f"      ! iface {nb_iface.name}  → ERROR: {e}")
                    loging(f"[{device_name}] Iface update error {nb_iface.name}: {e}", "error")
            else:
                skipped += 1
                loging(f"[{device_name}] Iface skip (ok): {nb_iface.name}", "debug")
        else:
            # --- Не найден → создаём ---
            iface_type = guess_nb_iface_type(zbx_name)
            create_data = {
                "device":      device.id,
                "name":        zbx_name[:64],
                "type":        iface_type,
                "enabled":     True,
                "description": zbx_alias[:200] if zbx_alias else "",
            }
            if ZABBIX_TAG:
                create_data["tags"] = [ZABBIX_TAG.id]

            try:
                netbox_api.dcim.interfaces.create(create_data)
                print(f"      + iface {zbx_name} ({iface_type})  descr='{zbx_alias[:40]}'  → created")
                loging(f"[{device_name}] Iface created: {zbx_name} type={iface_type}", "sync")
                created += 1
            except Exception as e:
                print(f"      ! iface {zbx_name}  → CREATE ERROR: {e}")
                loging(f"[{device_name}] Iface create error {zbx_name}: {e}", "error")

    # --- Disabled: интерфейсы с тегом zbb, которые не пришли из Zabbix ---
    disabled_count = 0
    for nb_iface in nb_ifaces_raw:
        if nb_iface.id in matched_nb_ids:
            continue
        # Только если есть тег zbb
        iface_tags = nb_iface.tags or []
        iface_tag_ids = _safe_tag_ids(iface_tags)
        has_zbb = (ZABBIX_TAG and ZABBIX_TAG.id in iface_tag_ids) or \
                  any(hasattr(t, 'name') and t.name == "zbb" for t in iface_tags)
        if not has_zbb:
            continue
        if nb_iface.enabled:
            try:
                nb_iface.update({"enabled": False})
                print(f"      - iface {nb_iface.name}  → disabled (not in Zabbix)")
                loging(f"[{device_name}] Iface disabled: {nb_iface.name}", "sync")
                disabled_count += 1
            except Exception as e:
                print(f"      ! iface {nb_iface.name}  → disable ERROR: {e}")
                loging(f"[{device_name}] Iface disable error {nb_iface.name}: {e}", "error")

    if skipped and not created and not updated and not disabled_count:
        print(f"      = интерфейсы → ok (no changes)")

    loging(f"[{device_name}] Iface summary: created={created} updated={updated} "
           f"skipped={skipped} disabled={disabled_count}", "sync")


# ═══════════════════════════════════════════════════════════════════════════════
# Синхронизация одного устройства
# ═══════════════════════════════════════════════════════════════════════════════

# Глобальное состояние для интерактивного режима создания устройств
_create_device_mode = None  # None = спрашивать, "all" = создавать все, "skip" = пропускать все


def _ask_create_device(name):
    """Спрашивает пользователя о создании устройства. Возвращает True/False."""
    global _create_device_mode

    if _create_device_mode == "all":
        return True
    if _create_device_mode == "skip":
        return False

    while True:
        choice = input(f"  [?] Устройство '{name}' не найдено в NetBox. Создать? [y/n/all/skip]: ").strip().lower()
        if choice == "y":
            return True
        if choice == "n":
            return False
        if choice == "all":
            _create_device_mode = "all"
            return True
        if choice == "skip":
            _create_device_mode = "skip"
            return False
        print("  [!] Введите y, n, all или skip")


def sync_net_device(hostid, group_name=None):
    """Синхронизирует одно сетевое устройство: данные + интерфейсы."""
    from common import ZABBIX_TAG

    data = get_net_host_data(hostid)
    name = data["hostname"].split(".")[0]

    # --- Логируем источник serial ---
    if data["serial"]:
        loging(f"[{name}] Serial source: {data['serial_source']} = {data['serial']}", "debug")
    else:
        loging(f"[{name}] Serial NOT FOUND: checked system.hw.serialnumber, huawei.serial, "
               f"system.serialnumber, inventory.serialno_a, inventory.serialno_b", "error")

    # --- Ищем устройство в NetBox ---
    device = nb_find_device(name)

    if not device:
        if not _ask_create_device(name):
            print(f"      → пропуск (не создаём)")
            loging(f"[{name}] Device not found, user skipped creation", "sync")
            return

        # --- Создание устройства ---
        # Site
        site = find_site_by_tag(data["site_tag"])
        if not site:
            fallback_site = cfg.get("net_default_site")
            if fallback_site:
                site = find_site_by_tag(fallback_site)
            if not site:
                print(f"      ! Не удалось определить site для '{name}' (тег site='{data['site_tag']}')")
                loging(f"[{name}] Cannot create device: site not found (tag='{data['site_tag']}')", "error")
                return

        # Device role
        role_name = group_name or (data["group_names"][0] if data["group_names"] else None)
        if not role_name:
            role_name = cfg.get("net_default_role", "Network")
        role = get_or_create_device_role(role_name)
        if not role:
            loging(f"[{name}] Cannot create device: role creation failed", "error")
            return

        # Device type
        device_type = None
        if data["model"]:
            device_type = get_or_create_device_type(data["model"])
        if not device_type:
            fallback_dt = cfg.get("net_default_device_type")
            if fallback_dt:
                device_type = get_or_create_device_type(fallback_dt)
        if not device_type:
            print(f"      ! Не удалось определить device_type для '{name}' (model='{data['model']}')")
            loging(f"[{name}] Cannot create device: device_type not found", "error")
            return

        # Создаём
        create_data = {
            "name":        name,
            "device_type": device_type.id,
            "role":        role.id,
            "site":        site.id,
            "status":      "active",
        }
        if data["serial"]:
            create_data["serial"] = data["serial"]
        if ZABBIX_TAG:
            create_data["tags"] = [ZABBIX_TAG.id]

        try:
            device = netbox_api.dcim.devices.create(create_data)
            print(f"      + device [{name}] created (site={site.name}, role={role.name}, type={device_type.model})")
            loging(f"[{name}] Device created: site={site.name}, role={role.name}, type={device_type.model}", "sync")
        except Exception as e:
            print(f"      ! device [{name}] CREATE ERROR: {e}")
            loging(f"[{name}] Device create error: {e}", "error")
            return

    # --- Обновление полей устройства ---
    update_data    = {}
    changed_fields = []

    # Serial
    if data["serial"]:
        old_serial = (device.serial or "").strip()
        if old_serial != data["serial"]:
            update_data["serial"] = data["serial"]
            changed_fields.append(f"serial: {old_serial or '∅'} → {data['serial']}")
        else:
            loging(f"[{name}] skip serial (no changes)", "debug")

    # Platform
    if data["model"]:
        platform = get_or_create_platform(data["model"])
        if platform:
            old_platform = device.platform.name if device.platform else "∅"
            if not device.platform or device.platform.id != platform.id:
                update_data["platform"] = platform.id
                changed_fields.append(f"platform: {old_platform} → {data['model']}")
            else:
                loging(f"[{name}] skip platform (no changes)", "debug")

    # Tags
    current_tags = list(device.tags) if device.tags else []
    current_tag_ids = _safe_tag_ids(current_tags)
    if ZABBIX_TAG and ZABBIX_TAG.id not in current_tag_ids:
        new_tag_list = _safe_tag_list_for_update(current_tags)
        new_tag_list.append(ZABBIX_TAG.id)
        update_data["tags"] = new_tag_list
        changed_fields.append("tag: +zbb")

    # Comments (ZBX-блок)
    if data["description"]:
        current_comments = (device.comments or "").strip()
        new_zbx_text     = compact_text(data["description"])
        existing_zbx     = extract_zbx_block_text(current_comments)
        if existing_zbx != new_zbx_text:
            update_data["comments"] = inject_zbx_block(current_comments, new_zbx_text)
            changed_fields.append("comments: zbx-block updated")
        else:
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

    # --- Синхронизация интерфейсов ---
    zbx_ifaces = get_net_interfaces_from_zabbix(hostid)
    sync_interfaces(device, zbx_ifaces, name)


# ═══════════════════════════════════════════════════════════════════════════════
# Точка входа
# ═══════════════════════════════════════════════════════════════════════════════

def run(groups=None):
    """
    Запускает синхронизацию сетевых устройств.
    groups — список групп (из select_groups()), если None — спрашивает интерактивно.
    """
    global _create_device_mode
    _create_device_mode = None  # Сбрасываем при каждом запуске

    if groups is None:
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

    loging("=" * 50, "sync")
    loging("Start sync: network devices", "sync")

    for group in groups:
        group_name = group["groupname"]
        loging(f"Processing group: {group_name}", "sync")
        print(f"\n[>] Группа: {group_name} ({len(group['hosts'])} хостов)")

        for host in group["hosts"]:
            hostid = host["hostid"]

            loging(f"Processing host: {host['name']}", "sync")
            print(f"    - {host['name']}")
            sync_net_device(hostid, group_name=group_name)

    loging("Done: network devices", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация сетевых устройств завершена.")


if __name__ == "__main__":
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        raise SystemExit(1)
    run()
