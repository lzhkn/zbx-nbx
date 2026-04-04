# zbx-nbx

Синхронизация инфраструктуры **Zabbix → NetBox**: устройства, диски, виртуальные машины (Proxmox VE, KVM), сетевое оборудование.

## Модули

| Файл | Режим в main.py | Назначение |
|---|---|---|
| `main.py` | — | Интерактивная точка входа (меню режимов) |
| `common.py` | — | Конфигурация, API, утилиты, интерактивный ввод |
| `sync_inventory.py` | 1 — Устройства | Serial, platform, tags, comments серверов |
| `sync_hardware.py` | 2 — Диски | Физические диски как inventory items (SMART + LSI) |
| `sync_vm_pve.py` | 3 — VM Proxmox | QEMU + LXC с дисками, интерфейсами, MAC (прямое подключение к PVE API) |
| `sync_vm_kvm.py` | 4 — VM KVM | KVM VM через Zabbix items (без прямого подключения) |
| `sync_network.py` | 5 — Сетевые | Serial, физические интерфейсы, descriptions сетевых устройств |

## Быстрый старт

1. Установить зависимости:
```bash
pip install pynetbox zabbix-utils proxmoxer urllib3
```

2. Создать `config.ini` рядом со скриптами:
```ini
[ZABBIX]
url   = https://zabbix.example.com
token = your_zabbix_api_token

[NETBOX]
url   = https://netbox.example.com
token = your_netbox_api_token
```

3. Запустить:
```bash
python main.py
```

## Полная конфигурация

```ini
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

[NETWORK]
default_role        = Network
default_site        = default
default_device_type = Generic Network Device
```

## Поддерживаемые вендоры (сетевой модуль)

Juniper, Cisco, Eltex, Huawei, UserGate, Arista, MikroTik, Fortinet, Mellanox, Dell, HP/HPE, Aruba, ZTE, Extreme, Brocade.

## Общие принципы

**Тег `zbb`** — маркер объектов, управляемых синхронизацией. Позволяет отличить созданные/обновлённые объекты от ручных.

**ZBX-блок** — описание из Zabbix хранится в `comments` устройства NetBox внутри маркеров `== zabbix description ==`, не затирая пользовательские комментарии.

**Защита данных** — если источник (PVE API, Zabbix) вернул пустой список дисков/интерфейсов, синхронизация пропускается (существующие данные в NetBox не удаляются).

**Get-or-create** — все вспомогательные объекты NetBox (tags, platforms, manufacturers, device roles, cluster types) создаются автоматически при первом обращении.

**Retry** — вызовы NetBox API автоматически повторяются при 502/503/504.

## Логирование

Лог-файлы создаются в рабочей директории:

| Файл | Содержимое |
|---|---|
| `sync_YYYY-MM-DD.log` | Основные операции: создание, обновление, удаление |
| `error_YYYY-MM-DD.log` | Ошибки API, не найденные объекты |
| `debug_YYYY-MM-DD.log` | Подробная диагностика: пропуски, матчинг интерфейсов |

## Документация модулей

Подробные README по каждому модулю:

- [main.py](README_main.md)
- [common.py](README_common.md)
- [sync_inventory.py](README_sync_inventory.md)
- [sync_hardware.py](README_sync_hardware.md)
- [sync_vm_pve.py](README_sync_vm_pve.md)
- [sync_vm_kvm.py](README_sync_vm_kvm.md)
- [sync_network.py](README_sync_network.md)
