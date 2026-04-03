# sync_network.py

Модуль синхронизации сетевых устройств **Zabbix → NetBox**.

Обрабатывает хосты из групп Zabbix (`Net/*`, `Net/Spine`, `Net/Leaf` и т.д.), создаёт/обновляет устройства и их физические интерфейсы в NetBox.

## Поддерживаемые вендоры

Juniper, Cisco, Eltex, Huawei, UserGate, Arista, MikroTik, Fortinet, Mellanox, Dell, HP/HPE, Aruba, ZTE, Extreme, Brocade.

## Что синхронизируется

**Устройство (device):**

| Поле          | Источник в Zabbix          | Примечания |
|---------------|----------------------------|---|
| `serial`      | items → inventory fallback | Цепочка: `system.hw.serialnumber` → `huawei.serial` → `system.serialnumber` → `inventory.serialno_a/b` |
| `platform`    | item `system.hw.model` → inventory `model` / `hardware_full` / `system` | Создаётся автоматически через `get_or_create_platform` |
| `device_type` | То же что platform     | Автоопределение manufacturer по имени модели |
| `device_role` | Имя группы Zabbix      | Fallback: конфиг `net_default_role` |
| `site`        | Тег `site` на хосте    | Поиск по name и slug в NetBox |
| `tags`        | — | Автоматически добавляется тег `zbb`                                 |
| `comments`    | `description` узла сети        | Вставляется как ZBX-блок (не затирает остальные комментарии) |

**Интерфейсы (interfaces):**

| Действие   | Условие |
|------------|-----------------------------|
| Создание   | Есть в Zabbix, нет в NetBox |
| Обновление | Есть в обоих — синхронизируется description, тег `zbb`, enabled |
| Disabled   | Есть в NetBox (с тегом `zbb`), нет в Zabbix → `enabled=false` |

## Нормализация имён интерфейсов

Интерфейсы из Zabbix и NetBox могут называться по-разному. Модуль приводит их к каноническому виду для матчинга:

```
GigabitEthernet0/0/1  →  gi:0/0/1
ge-0/0/1              →  gi:0/0/1
Gi0/1                 →  gi:0/1
GE0/0/1               →  gi:0/0/1
xe-0/0/58:3           →  te:0/0/58:3
TenGigE0/0/0/1        →  te:0/0/0/1
```

**Каноническая форма → тип NetBox:**

| Int  | speed | Тип NetBox          |
|------|-----------------------------|
| `fa` | 100M  | `100base-tx`        |
| `gi` | 1G    | `1000base-t`        |
| `te` | 10G   | `10gbase-x-sfpp`    |
| `tf` | 25G   | `25gbase-x-sfp28`   |
| `fo` | 40G   | `40gbase-x-qsfpp`   |
| `hu` | 100G  | `100gbase-x-qsfp28` |


### Cisco stack fallback

Cisco при переходе на IOS-XE или при добавлении в стек меняет нотацию: 
`Gi0/48` → `Gi1/0/48` (добавляется номер stack member). Модуль обрабатывает это автоматически — если `gi:1/0/48` не найден, пробует `gi:0/48`, и наоборот.

### Фильтрация интерфейсов

Отсекаются нефизические интерфейсы (Vlan, Loopback, Port-channel, Tunnel, и т.д.) и субинтерфейсы (логические unit с точкой: `xe-0/0/58:3.918`, `xe-0/0/58:3.32767`). Физические каналы с двоеточием (`xe-0/0/58:3`) проходят нормально.

Дополнительно фильтруется по SNMP ifType — пропускаются всё, кроме `6` (ethernetCsmacd).

## Запуск

**Через main.py :**

```bash
python main.py
```

**Напрямую:**

```bash
python sync_network.py
```

При запуске модуль интерактивно спрашивает какие группы Zabbix обрабатывать. Для устройств, не найденных в NetBox, предлагает создать — с вариантами `y` / `n` / `all` / `skip`.

## Зависимости

Модуль импортирует из `common.py`:

| Объект           | Назначение 
|------------------|-----------------
| `cfg`            | Конфигурация (yaml) 
| `zabbix_api`     | Подключение к Zabbix API 
| `netbox_api`     | Подключение к NetBox API (pynetbox) 
| `ZABBIX_TAG`     | Объект тега `zbb` в NetBox 
| `loging`         | Логирование в файл 
| `slugify`        | Генерация slug для NetBox 
| `compact_text`   | Очистка текста 
| `inject_zbx_block` / `extract_zbx_block_text` | Работа с ZBX-блоком в comments 
| `get_or_create_tag` / `get_or_create_platform` | Создание тегов и платформ 
| `nb_find_device` | Поиск устройства в NetBox 
| `select_groups`  | Интерактивный выбор групп Zabbix 
| `init_resources` | Инициализация подключений и ресурсов 

## Конфигурация (config.yaml)

| Параметр | Описание | По умолчанию |
|----------|----------|--------------|
| `net_default_site`  | Fallback site если тег `site` не найден | — |
| `net_default_role`  | Fallback device role | `Network` |
| `net_default_device_type` | Fallback device type если модель не определена | — |

## Структура модуля

```
sync_network.py
│
├── Нормализация интерфейсов
│   ├── IFACE_NORMALIZE_MAP          # Таблица маппинга имён → canonical
│   ├── IFACE_SKIP_PATTERNS          # Паттерны нефизических интерфейсов
│   ├── CANONICAL_TO_NB_TYPE         # Canonical → тип NetBox
│   ├── normalize_iface_name()       # Нормализация имени
│   ├── is_physical_iface()          # Фильтр: физический ли интерфейс
│   └── guess_nb_iface_type()        # Определение типа для NetBox
│
├── Данные из Zabbix
│   ├── get_item_value()             # Значение item по ключу
│   ├── get_net_host_data()          # Все данные хоста (serial, model, site, ...)
│   ├── get_net_interfaces_from_zabbix()  # Список физических интерфейсов
│   ├── _parse_iface_name_alias_from_item_name()  # Парсинг имени item
│   └── _extract_snmpindex_from_key() # Извлечение SNMP index
│
├── NetBox: get-or-create
│   ├── get_or_create_manufacturer() # Manufacturer (поиск по name+slug, retry)
│   ├── get_or_create_device_type()  # Device type (auto manufacturer)
│   ├── get_or_create_device_role()  # Device role (поиск по name+slug, retry)
│   ├── find_site_by_tag()           # Поиск site по name/slug
│   └── guess_manufacturer()         # Авто-определение вендора по модели
│
├── Синхронизация
│   ├── sync_interfaces()            # Интерфейсы: create/update/disable
│   └── sync_net_device()            # Одно устройство: данные + интерфейсы
│
└── Точка входа
    └── run()                        # Цикл по группам и хостам
```

## Логика получения интерфейсов из Zabbix

1. Запрос всех items с ключами `net.if.*` и `ifOperStatus[*]`
2. Из них выделяются «якорные» items — `net.if.status[ifOperStatus.INDEX]` — один на интерфейс
3. Из поля `name` якорного item парсится имя и alias:
   `"Interface GigabitEthernet0/0/1(uplink-to-core): Operational status"` → name=`GigabitEthernet0/0/1`, alias=`uplink-to-core`
4. По тому же SNMP index подтягиваются ifType и ifHighSpeed из соседних items
5. Если alias пустой — пробуем найти в других items того же интерфейса
6. Фильтрация: только физические (ifType=6, не в skip-списке, не субинтерфейс)

### Форматы item name

| Вендор   | Формат | Пример |
|----------|--------|--------|
| Cisco, Juniper, Huawei, Eltex | `Interface NAME(ALIAS): metric` | `Interface xe-0/0/19(r): Operational status` |
| Cisco (без alias) | `Interface NAME: metric` | `Interface Gi0/1: Bits received` |
| UserGate | `... of interface NAME` | `Operational status of interface eth0` |
| UserGate | `... on interface NAME` | `Speed on interface eth0` |

Парсинг alias устойчив к скобкам внутри alias — ищется последнее `): ` как разделитель. Например: `Interface xe-0/0/19(OpenVAS (scanner)): ...` → name=`xe-0/0/19`, alias=`OpenVAS (scanner)`.

## Логика матчинга интерфейсов (Zabbix ↔ NetBox)

Порядок приоритета при поиске существующего интерфейса в NetBox:

1. **Точное совпадение имени** — `zbx_name == nb_iface.name`
2. **Нормализованный матчинг** — оба имени приводятся к canonical form (`gi:0/0/1`) и сравниваются
3. **Cisco stack fallback** — `gi:0/48` ↔ `gi:1/0/48` (2-компонентный ↔ 3-компонентный)

## Changelog

### v4 (текущая)

- **Tag safety** — `_safe_tag_ids()` / `_safe_tag_list_for_update()`: pynetbox может вернуть теги как int или как объекты, теперь обрабатываются оба варианта. Исправлен `AttributeError: 'int' object has no attribute 'id'`.
- **Субинтерфейсы** — `is_physical_iface()` отсекает logical unit (`.NNN` на конце): `xe-0/0/58:3.918`, `xe-0/0/58:3.32767`. Физические каналы с двоеточием (`xe-0/0/58:3`) проходят.
- **Cisco stack fallback** — матчинг `Gi0/48` ↔ `Gi1/0/48` при переходе IOS → IOS-XE или добавлении в стек.

### v3

- **Парсинг имени интерфейса** — переписан `_parse_iface_name_alias_from_item_name()`, устойчив к скобкам в alias.
- **Device role / Manufacturer** — get_or_create поиск по name + slug, retry после ошибки.
- **Interface name** — обрезка до 64 символов (лимит NetBox).
- **Serial** — расширенная цепочка: `huawei.serial`, `selectInventory="extend"`, fallback `serialno_a/b`.
