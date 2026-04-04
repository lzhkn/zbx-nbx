# sync_vm_pve.py

Синхронизация виртуальных машин **Proxmox VE → NetBox**: QEMU VM и LXC-контейнеры с дисками, интерфейсами и MAC-адресами.

## Источник данных

Модуль подключается **напрямую к Proxmox VE API** (библиотека `proxmoxer`), а не через Zabbix items. Креденшены для подключения берутся из макросов Zabbix-хоста с шаблоном `Proxmox VE by HTTP`.

## Что синхронизируется

### Virtual Machine

| Поле NetBox | Источник PVE |
|---|---|
| `name` | `config.name` |
| `cluster` | Кластер NetBox = имя ноды PVE |
| `status` | `running` → active, `stopped` → offline, `paused` → planned, template → staged |
| `vcpus` | `config.cores` |
| `memory` | `config.memory` (MB) |
| `serial` | VMID (строка) |
| `role` | Из конфига `[PROXMOX] role_vm` |
| `device` | Привязка к физическому хосту (device в NetBox) |
| `comments` | `config.description` |
| `tags` | `zbb` + теги из PVE (разделитель `;`) |

### Диски (virtual_disks)

| Поле NetBox | Источник |
|---|---|
| `name` | Путь: `{node}/{storage:volume}` |
| `size` | Из параметра `size=` в конфиге (MB) |

Парсятся ключи конфига: `scsi*`, `ide*` (QEMU) и `rootfs`, `mp*` (LXC). CD-ROM пропускается.

### Интерфейсы (vm interfaces)

| Поле NetBox | Источник |
|---|---|
| `name` | Ключ конфига: `net0`, `net1`, ... |
| `enabled` | `true` если нет `link_down` в конфиге |
| MAC-адрес | Из параметров `virtio=`, `e1000e=`, `hwaddr=` |

MAC-адреса создаются как объекты `dcim.mac_addresses` и привязываются к интерфейсу как `primary_mac_address`.

## Архитектура кластеров

Каждая нода PVE = отдельный кластер NetBox (тип `Proxmox VE`). Device с именем ноды привязывается к кластеру.

Если PVE-ноды входят в реальный PVE-кластер (определяется через `cluster.status`), обходятся **все** ноды кластера. Для standalone-нод — только выбранные.

## Обработка исчезнувших VM

VM, которые есть в NetBox (в кластере ноды) но отсутствуют в PVE, обрабатываются по выбору пользователя:

| Режим | Действие |
|---|---|
| `delete` | Удалить из NetBox |
| `offline` | Перевести в статус offline |

## Защита данных

Если PVE API вернул пустой список дисков или интерфейсов для VM, синхронизация этих сущностей пропускается (не удаляет существующие в NetBox). Это защита от случайного удаления при временных ошибках API.

## Получение креденшенов PVE

Из Zabbix макросов хоста с шаблоном `Proxmox VE by HTTP`:

| Макрос | Назначение |
|---|---|
| `{$PVE.URL.HOST}` | Адрес PVE-ноды |
| `{$PVE.URL.PORT}` | Порт (по умолчанию 8006) |
| `{$PVE.TOKEN.ID}` | `user@realm!tokenname` |
| `{$PVE.TOKEN.SECRET}` | Секрет токена |

Макросы наследуются от шаблона с возможностью переопределения на уровне хоста.

## Структура

```
sync_vm_pve.py
│
├── Парсинг конфигов PVE
│   ├── parse_mac_from_iface()        # MAC из строки net-интерфейса
│   ├── vm_pve_status_to_nb()         # Маппинг статусов PVE → NetBox
│   ├── parse_disk_size_mb()          # Парсинг размера (T/G/M)
│   ├── parse_vm_disks()              # Диски QEMU из конфига
│   ├── parse_vm_interfaces()         # Интерфейсы QEMU из конфига
│   ├── parse_lxc_disks()             # Диски LXC (rootfs + mp*)
│   └── parse_lxc_interfaces()        # Интерфейсы LXC (net* + hwaddr)
│
├── NetBox: кластеры и MAC
│   ├── get_or_create_pve_cluster_for_node()  # Кластер per-нода + привязка device
│   └── _assign_mac()                          # Создание/переназначение MAC-адреса
│
├── Синхронизация сущностей
│   ├── sync_vm_disks_nb()            # Диски VM: create/update/delete
│   └── sync_vm_interfaces_nb()       # Интерфейсы VM: create/update/delete + MAC
│
├── Выбор PVE-хостов
│   ├── get_pve_hosts_from_zabbix()   # Список PVE-нод из Zabbix (по шаблону + макросы)
│   └── select_pve_clusters()          # Интерактивный выбор (glob + номера)
│
├── Основная синхронизация
│   └── sync_pve_cluster()            # Полный цикл: ноды → QEMU → LXC → missing VM
│
└── Точка входа
    └── run()                         # Запуск с параметрами
```

## Запуск

```bash
python main.py        # режим 3
python sync_vm_pve.py  # напрямую
```

## Конфигурация (config.ini)

| Параметр | Секция | Описание |
|---|---|---|
| `template_id` | `[PROXMOX]` | ID шаблона `Proxmox VE by HTTP` в Zabbix |
| `role_vm` | `[PROXMOX]` | ID роли VM в NetBox |
| `domain` | `[PROXMOX]` | Домен для поиска device по FQDN |

## Зависимости

Внешние: `proxmoxer`.

Из common.py:
```
cfg, zabbix_api, netbox_api,
ZABBIX_TAG,
loging, compact_text,
get_or_create_tag, get_or_create_cluster_type,
nb_find_device,
select_groups, select_missing_vm_behavior,
_handle_missing_vm, init_resources
```
