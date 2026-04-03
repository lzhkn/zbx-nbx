# zbx-nbx

Синхронизация данных из **Zabbix** → **NetBox**.

---

## Структура проекта

```
zbx-nbx/
├── config.ini          # конфигурация (создать вручную, не коммитить)
├── common.py           # общие утилиты, API-инициализация, конфиг
├── main.py             # интерактивный запуск (точка входа)
├── sync_inventory.py   # режим 1: устройства (serial, platform, tags, comments)
├── sync_hardware.py    # режим 2: диски (inventory items)
├── sync_vm_pve.py      # режим 3: VM Proxmox VE (QEMU + LXC)
└── sync_vm_kvm.py      # режим 4: VM KVM
```

Каждый `sync_*.py` можно запускать как самостоятельно, так и через `main.py`.

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/yourorg/zbx-nbx.git
cd zbx-nbx
```

### 2. Создать виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate       # Linux / macOS
# venv\Scripts\activate        # Windows
```

### 3. Установить зависимости

```bash
pip install pynetbox zabbix_utils proxmoxer urllib3
```

### 4. Создать конфиг

Скопировать пример и заполнить:

```bash
cp config.example.ini config.ini
```

Или создать `config.ini` вручную — см. раздел [Конфигурация](#конфигурация) ниже.

---

## Конфигурация

Файл `config.ini` в корне проекта.

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
```

### Параметры

**[ZABBIX]**
| Параметр | Описание |
|----------|----------|
| `url` | Адрес Zabbix (с протоколом) |
| `token` | API-токен (Zabbix 5.4+) |

**[NETBOX]**
| Параметр | Описание |
|----------|----------|
| `url` | Адрес NetBox (с протоколом) |
| `token` | API-токен NetBox |

**[PROXMOX]** — нужен только для `sync_vm_pve.py`
| Параметр | Описание |
|----------|----------|
| `template_id` | ID шаблона Zabbix "Proxmox VE by HTTP" |
| `role_vm` | ID роли NetBox для VM (Virtualization → Roles) |
| `domain` | Суффикс домена нод, например `.example.com` (опционально) |

**[KVM]** — нужен только для `sync_vm_kvm.py`
| Параметр | Описание |
|----------|----------|
| `template_id` | ID шаблона Zabbix "Template KVM" |
| `role_vm` | ID роли NetBox для VM |

---

## Запуск

### Через главное меню

```bash
python3 main.py
```

Интерактивное меню предложит выбрать режим:

```
==================================================
  Что синхронизируем?
==================================================
  1. Устройства    (serial, platform, tags, comments)
  2. Диски         (inventory items: serial, model, status)
  3. VM Proxmox    (QEMU + LXC → NetBox)
  4. VM KVM        (KVM → NetBox через Zabbix items)
==================================================
```

### Напрямую (без меню)

Каждый скрипт можно запустить отдельно — он сам задаст нужные вопросы:

```bash
python3 sync_inventory.py   # только устройства
python3 sync_hardware.py    # только диски
python3 sync_vm_pve.py      # только PVE VM
python3 sync_vm_kvm.py      # только KVM VM
```

---

## Режимы синхронизации

### 1. Устройства (`sync_inventory.py`)

Обрабатывает хосты Zabbix с шаблонами `Linux by Zabbix agent` и `Proxmox VE by HTTP`.

Что обновляется в NetBox `device`:
- `serial` — из item `dmidecode.SerialNumber` или inventory `serialno_a`
- `platform` — из item `os.system.product_name` или inventory `system` (создаётся автоматически)
- `tags` — добавляется тег `zbb`
- `comments` — описание Zabbix вставляется в блок между маркерами `== zabbix description ==`; текст пользователя вне блока не затрагивается

### 2. Диски (`sync_hardware.py`)

Источники в Zabbix: `smart.disk.sn[*]` / `smart.disk.model[*]` и `lsi.pd.sn[*]` / `lsi.pd.model[*]`.

Что создаётся/обновляется в NetBox `inventory item` устройства:
- `name` — имя устройства (sda, sdb, ...)
- `serial` — серийный номер диска
- `part_id` — модель диска
- `status` — `active` если виден в Zabbix, `offline` если только в NetBox
- `role` — роль `Disks`
- `tags` — тег `zbb`

### 3. VM Proxmox VE (`sync_vm_pve.py`)

**Кластер per-нода:** каждая PVE-нода = отдельный кластер NetBox (имя = имя ноды, тип "Proxmox VE"). `device.cluster` ноды привязывается к нему.

**Реальный PVE-кластер** определяется автоматически через `cluster.status` — все ноды обходятся через единую точку входа.

Что создаётся/обновляется в NetBox `virtual machine`:
- `name` — имя VM из конфига PVE
- `serial` — vmid (числовой ID VM в PVE)
- `status` — `active` / `offline` / `planned` / `staged`
- `vcpus`, `memory`
- `device` — привязка к физической ноде
- `cluster` — кластер ноды
- `tags` — `zbb` + теги из конфига PVE
- `comments` — описание из конфига PVE
- `virtual_disks` — диски (scsi/ide для QEMU, rootfs/mp для LXC)
- `interfaces` — сетевые интерфейсы + MAC-адреса

### 4. VM KVM (`sync_vm_kvm.py`)

**Кластер per-гипервизор:** каждый KVM-хост = отдельный кластер NetBox (имя = имя гипервизора, тип "KVM"). `device.cluster` привязывается.

Данные читаются из Zabbix items шаблона KVM:

| Item | Тип | Содержимое |
|------|-----|------------|
| `vmstatus.status[VMNAME]` | dependent LLD | статус VM |
| `vmstatistic_cpu_mem` | RAW TEXT JSON | vCPU и RAM |
| `vm_blk_discovery` | RAW TEXT JSON | диски |
| `vmlist_network` | RAW TEXT JSON | интерфейсы и MAC |

**Защита от пустых данных:** если Zabbix item не собрал данные по дискам или интерфейсам — существующие записи в NetBox не удаляются.

---

## Поведение при исчезнувших VM

При запуске режимов 3 и 4 скрипт спрашивает:

```
y — Удалить из NetBox
n — Оставить, перевести в статус offline (для истории)
```

---

## Логи

Пишутся в текущую директорию:

| Файл | Содержимое |
|------|------------|
| `sync_YYYY-MM-DD.log` | создание, обновление, пропуск |
| `error_YYYY-MM-DD.log` | ошибки API |
| `debug_YYYY-MM-DD.log` | детальные пропуски (no changes) |

---

## Ресурсы создаваемые автоматически

При первом запуске скрипт создаёт в NetBox:

| Объект | Значение |
|--------|----------|
| Тег | `zbb` (зелёный) |
| Роль inventory items | `Disks` (синяя) |
| Платформы | по данным из Zabbix |
| Тип кластера | `Proxmox VE`, `KVM` |
| Кластеры | по одному на каждую PVE-ноду и KVM-гипервизор |

---

## Примечания

- SSL-верификация отключена (self-signed сертификаты)
- NetBox API: retry при 502/503/504 — 3 попытки с паузой 5 сек
- Тег `zbb` только добавляется, никогда не удаляется скриптом
- Комментарии `device` вне ZBX-блока скриптом не затрагиваются
- Диски устройств при исчезновении → `offline` (не удаляются)
- `config.ini` не должен попадать в git — добавьте в `.gitignore`
