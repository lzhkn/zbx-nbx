# sync_vm_kvm.py

Синхронизация виртуальных машин **KVM → NetBox** через Zabbix items (без прямого подключения к гипервизору).

## Источник данных

В отличие от `sync_vm_pve`, этот модуль **не подключается к гипервизору напрямую**. Все данные читаются из Zabbix items, которые заполняются кастомным шаблоном KVM-мониторинга.

## Что синхронизируется

### Virtual Machine

| Поле NetBox | Источник в Zabbix |
|---|---|
| `name` | Из ключа item `vmstatus.status[VMNAME]` |
| `cluster` | Кластер NetBox = имя гипервизора |
| `status` | running → active, shut off → offline, paused → planned, crashed → failed |
| `vcpus` | JSON item `vmstatistic_cpu_mem` → поле `nrVirtCpu` |
| `memory` | JSON item `vmstatistic_cpu_mem` → поле `actual` (bytes → MB) |
| `role` | Из конфига `[KVM] role_vm` |
| `device` | Привязка к физическому хосту |
| `tags` | `zbb` |

### Диски (virtual_disks)

| Поле NetBox | Источник |
|---|---|
| `name` | `{target}:{source}` из JSON item `vm_blk_discovery` |
| `size` | Dependent item `disk.Capacity[VMNAME,TARGET]` (bytes → MB) |

CD-ROM и floppy пропускаются.

### Интерфейсы (vm interfaces)

| Поле NetBox | Источник |
|---|---|
| `name` | Поле `Interface` из JSON item `vmlist_network` |
| MAC-адрес | Поле `MAC` |
| `enabled` | Всегда `true` |

## Источники данных в Zabbix (items)

| Item ключ | Тип | Описание |
|---|---|---|
| `vmstatus.status[VMNAME]` | Dependent (LLD) | Статус каждой VM. Один item на VM |
| `vmstatistic_cpu_mem` | RAW JSON (master) | CPU и RAM всех VM на гипервизоре |
| `vm_blk_discovery` | RAW JSON (master) | Блочные устройства всех VM |
| `vmlist_network` | RAW JSON (master) | Сетевые интерфейсы всех VM |
| `disk.Capacity[VM,TARGET]` | Dependent | Размер конкретного диска |

### Формат JSON (vmstatistic_cpu_mem)

```json
{
  "data": [
    {"VMNAME": "web01", "actual": 4294967296, "nrVirtCpu": 4},
    {"VMNAME": "db01",  "actual": 8589934592, "nrVirtCpu": 8}
  ]
}
```

### Формат JSON (vm_blk_discovery)

```json
{
  "data": [
    {"VMNAME": "web01", "Target": "vda", "Source": "/dev/ssd-pool/web01", "Device": "disk"},
    {"VMNAME": "web01", "Target": "hdc", "Source": "",                    "Device": "cdrom"}
  ]
}
```

### Формат JSON (vmlist_network)

```json
{
  "data": [
    {"VMNAME": "web01", "Interface": "vnet0", "MAC": "52:54:00:aa:bb:cc"}
  ]
}
```

## Архитектура кластеров

Каждый KVM-гипервизор = отдельный кластер NetBox (тип `KVM`). Device с именем гипервизора привязывается к кластеру.

## Обработка исчезнувших VM

| Режим | Действие |
|---|---|
| `delete` | Удалить из NetBox |
| `offline` | Перевести в статус offline |

## Защита данных

Если Zabbix вернул пустой список дисков или интерфейсов для VM, синхронизация этих сущностей пропускается. Существующие данные в NetBox не удаляются.

## Структура

```
sync_vm_kvm.py
│
├── Выбор KVM-хостов
│   ├── get_kvm_hosts_from_zabbix()   # Список KVM-гипервизоров по шаблону
│   └── select_kvm_hosts()            # Интерактивный выбор (glob + номера)
│
├── Чтение данных из Zabbix
│   ├── get_kvm_raw_item()            # RAW JSON item → dict
│   ├── get_kvm_dependent_value()     # Dependent item → string
│   ├── kvm_status_to_nb()            # Маппинг статусов KVM → NetBox
│   ├── parse_kvm_vm_list()           # Список VM из vmstatus.status[*]
│   ├── parse_kvm_vm_resources()      # CPU/RAM из vmstatistic_cpu_mem
│   ├── parse_kvm_vm_disks()          # Диски из vm_blk_discovery
│   └── parse_kvm_vm_interfaces()     # Интерфейсы из vmlist_network
│
├── NetBox: кластеры и MAC
│   ├── get_or_create_kvm_cluster_for_device()  # Кластер per-гипервизор
│   └── _assign_mac()                            # Создание/переназначение MAC
│
├── Синхронизация сущностей
│   ├── sync_kvm_vm_disks()           # Диски VM: create/update/delete
│   └── sync_kvm_vm_interfaces()      # Интерфейсы VM: create/update/delete + MAC
│
├── Основная синхронизация
│   └── sync_kvm_host()               # Один гипервизор: VM list → ресурсы → sync → missing
│
└── Точка входа
    └── run()                         # Запуск с параметрами
```

## Запуск

```bash
python main.py        # режим 4
python sync_vm_kvm.py  # напрямую
```

## Конфигурация (config.ini)

| Параметр | Секция | Описание |
|---|---|---|
| `template_id` | `[KVM]` | ID кастомного KVM-шаблона в Zabbix |
| `role_vm` | `[KVM]` | ID роли VM в NetBox |

## Зависимости из common.py

```
cfg, zabbix_api, netbox_api,
ZABBIX_TAG,
loging,
get_or_create_tag, get_or_create_cluster_type,
nb_find_device,
select_groups, select_missing_vm_behavior,
_handle_missing_vm, init_resources
```
