#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# sync_vm_kvm.py — синхронизация VM KVM → NetBox через Zabbix items
# Запускается напрямую или через main.py
#
# Каждый KVM-гипервизор = отдельный кластер NetBox (имя = короткое имя device, тип = "KVM").
# Данные читаются из Zabbix items шаблона KVM:
#   vmstatus.status[VMNAME]  — статус VM (dependent LLD items)
#   vmstatistic_cpu_mem      — CPU и RAM (RAW JSON master item)
#   vm_blk_discovery         — диски (RAW JSON master item)
#   vmlist_network           — сетевые интерфейсы (RAW JSON master item)

import re
import json
import fnmatch

from common import (
    cfg, zabbix_api, netbox_api,
    ZABBIX_TAG,
    loging,
    get_or_create_tag, get_or_create_cluster_type,
    nb_find_device,
    select_groups, select_missing_vm_behavior,
    _handle_missing_vm, init_resources,
)


# --- Выбор KVM-хостов ---

def get_kvm_hosts_from_zabbix(template_id, allowed_hostids=None):
    hosts = zabbix_api.host.get(templateids=template_id, output=["hostid", "host", "name"])
    if allowed_hostids is not None:
        allowed_set = {str(h) for h in allowed_hostids}
        hosts = [h for h in hosts if str(h["hostid"]) in allowed_set]
    return [
        {"zabbix_name": h["host"], "hostid": h["hostid"], "display": h["name"]}
        for h in hosts
    ]


def select_kvm_hosts(template_id, allowed_hostids=None):
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


# --- Чтение данных из Zabbix ---

def get_kvm_raw_item(hostid, item_key):
    """Читает RAW TEXT item по точному ключу и парсит JSON."""
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
    """Читает lastvalue dependent item по точному ключу."""
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


def parse_kvm_vm_list(hostid, node_name):
    """Читает список VM через dependent items vmstatus.status[VMNAME]."""
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

        m = re.match(r'^vmstatus\.status\[(.+)\]$', key)
        if not m:
            loging(f"[KVM] unexpected key format: {key}", "debug")
            continue

        vm_name = m.group(1).strip()
        if not vm_name:
            continue

        result.append({
            "name":       vm_name,
            "status_nb":  kvm_status_to_nb(status),
            "status_raw": status,
        })
        loging(f"[KVM] {node_name}: VM={vm_name} status={status}", "debug")

    loging(f"[KVM] {node_name}: found {len(result)} VMs via vmstatus.status[]", "sync")
    return result


def parse_kvm_vm_resources(hostid, node_name):
    """Читает CPU и RAM через vmstatistic_cpu_mem."""
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
    """Читает диски через vm_blk_discovery."""
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
        if device.lower() in ("cdrom", "floppy"):
            continue

        cap_key = f"disk.Capacity[{vm_name},{target}]"
        cap_val = get_kvm_dependent_value(hostid, cap_key)
        size_mb = 0
        if cap_val:
            try:
                size_mb = int(float(cap_val)) // (1024 * 1024)
            except Exception:
                size_mb = 0

        disk_path = f"{target}:{source}" if source else target

        if vm_name not in result:
            result[vm_name] = []
        result[vm_name].append({"path": disk_path, "size_mb": size_mb})

    return result


def parse_kvm_vm_interfaces(hostid, node_name):
    """Читает сетевые интерфейсы через vmlist_network."""
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
            "enabled": True,
        })

    return result


# --- Кластер per-гипервизор ---

def get_or_create_kvm_cluster_for_device(node_name):
    """Получает/создаёт KVM-кластер и привязывает device."""
    nb_cluster = netbox_api.virtualization.clusters.get(name=node_name)
    if not nb_cluster:
        cluster_type = get_or_create_cluster_type("KVM")
        try:
            nb_cluster = netbox_api.virtualization.clusters.create(
                name=node_name, type=cluster_type.id, status="active"
            )
            loging(f"[KVM] Cluster created: {node_name}", "sync")
            print(f"  [+] Кластер NetBox создан: {node_name} (тип KVM)")
        except Exception:
            nb_cluster = netbox_api.virtualization.clusters.get(name=node_name)

    if not nb_cluster:
        loging(f"[KVM] Failed to get/create cluster for {node_name}", "error")
        return None

    device = nb_find_device(node_name)
    if device:
        if not device.cluster or device.cluster.id != nb_cluster.id:
            try:
                device.cluster = {"id": nb_cluster.id}
                device.save()
                loging(f"[KVM] Device {node_name} → cluster {node_name}", "sync")
                print(f"  [~] Device {node_name} привязан к кластеру {node_name}")
            except Exception as e:
                loging(f"[KVM] Device cluster bind error {node_name}: {e}", "error")
                print(f"  [!] Ошибка привязки device к кластеру: {e}")
    else:
        loging(f"[KVM] Device not found in NetBox: {node_name}", "error")
        print(f"  [!] Device '{node_name}' не найден в NetBox — кластер создан, device не привязан")

    return nb_cluster


# --- MAC ---

def _assign_mac(nb_iface, mac, all_macs_cache):
    if mac in all_macs_cache:
        mac_obj = netbox_api.dcim.mac_addresses.get(mac_address=mac)
        if mac_obj and mac_obj.assigned_object and mac_obj.assigned_object["id"] == nb_iface.id:
            return
        if mac_obj:
            mac_obj.delete()

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

    iface_fresh = netbox_api.virtualization.interfaces.get(id=nb_iface.id)
    if iface_fresh and iface_fresh.primary_mac_address is None:
        iface_fresh.primary_mac_address = {"id": mac_obj.id}
        try:
            iface_fresh.save()
        except Exception as e:
            loging(f"[MAC] primary_mac_address error: {e}", "error")


# --- Синхронизация дисков и интерфейсов ---

def sync_kvm_vm_disks(nb_vm, kvm_disks):
    """Синхронизирует диски KVM VM. Пустой список → пропуск (защита данных)."""
    if not kvm_disks:
        nb_count = len(list(netbox_api.virtualization.virtual_disks.filter(virtual_machine_id=nb_vm.id)))
        loging(f"[{nb_vm.name}] KVM disks: Zabbix returned empty — skip sync (protect {nb_count} existing)", "debug")
        print(f"      Дисков в KVM: 0  в NetBox: {nb_count}  → пропуск (Zabbix не вернул данные)")
        return

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
    """Синхронизирует интерфейсы KVM VM. Пустой список → пропуск (защита данных)."""
    if not kvm_ifaces:
        loging(f"[{nb_vm.name}] KVM ifaces: Zabbix returned empty — skip sync", "debug")
        return

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


# --- Синхронизация одного гипервизора ---

def sync_kvm_host(host_info, role_vm_id, all_macs_cache, missing_vm_behavior):
    """Синхронизирует все VM одного KVM-гипервизора с NetBox."""
    from common import ZABBIX_TAG  # актуальный глобал

    node_name = host_info["zabbix_name"].split(".")[0]
    hostid    = host_info["hostid"]

    print(f"\n  [>] KVM-гипервизор: {node_name} (hostid={hostid})")
    loging(f"[KVM] Processing host: {node_name}", "sync")

    nb_cluster = get_or_create_kvm_cluster_for_device(node_name)
    if not nb_cluster:
        print(f"  [!] Не удалось создать/получить кластер для {node_name}, пропускаем")
        return

    host_dev = nb_find_device(node_name)

    vm_list = parse_kvm_vm_list(hostid, node_name)
    if not vm_list:
        return

    resources  = parse_kvm_vm_resources(hostid, node_name)
    all_disks  = parse_kvm_vm_disks(hostid, node_name)
    all_ifaces = parse_kvm_vm_interfaces(hostid, node_name)

    print(f"    VM: {len(vm_list)},  с ресурсами: {len(resources)},  "
          f"с дисками: {len(all_disks)},  с интерфейсами: {len(all_ifaces)}")

    kvm_vm_names = set()

    for vm in vm_list:
        vm_name = vm["name"]
        kvm_vm_names.add(vm_name)

        nb_status  = vm["status_nb"]
        res        = resources.get(vm_name, {})
        memory_mb  = res.get("memory_mb", 0)
        vcpus      = res.get("vcpus", 0)
        kvm_disks  = all_disks.get(vm_name, [])
        kvm_ifaces = all_ifaces.get(vm_name, [])

        nb_vm = netbox_api.virtualization.virtual_machines.get(
            name=vm_name, cluster_id=nb_cluster.id
        )

        if nb_vm is None:
            create_data = {
                "name":    vm_name,
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
                loging(f"[{node_name}] KVM VM created: {vm_name}", "sync")
                print(f"    + {vm_name}  (status={vm['status_raw']}, vcpus={vcpus}, mem={memory_mb}MB)")
            except Exception as e:
                loging(f"[{node_name}] KVM VM create error {vm_name}: {e}", "error")
                continue
        else:
            changed = False
            changed_fields = []

            if nb_vm.status and nb_vm.status.value != nb_status:
                nb_vm.status = nb_status; changed = True; changed_fields.append(f"status→{nb_status}")
            else:
                loging(f"[{node_name}] KVM VM skip status (ok): {vm_name}", "debug")

            if vcpus and nb_vm.vcpus != vcpus:
                nb_vm.vcpus = vcpus; changed = True; changed_fields.append("vcpus")
            elif vcpus:
                loging(f"[{node_name}] KVM VM skip vcpus (ok): {vm_name}", "debug")

            if memory_mb and nb_vm.memory != memory_mb:
                nb_vm.memory = memory_mb; changed = True; changed_fields.append("memory")
            elif memory_mb:
                loging(f"[{node_name}] KVM VM skip memory (ok): {vm_name}", "debug")

            if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                nb_vm.role = role_vm_id; changed = True; changed_fields.append("role")

            if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                nb_vm.device = host_dev.id; changed = True; changed_fields.append("device")

            if not nb_vm.cluster or nb_vm.cluster.id != nb_cluster.id:
                nb_vm.cluster = nb_cluster.id; changed = True; changed_fields.append("cluster")

            current_tag_names = {t["name"] for t in (nb_vm.tags or [])}
            if ZABBIX_TAG and ZABBIX_TAG.name not in current_tag_names:
                nb_vm.tags = list(nb_vm.tags or []) + [ZABBIX_TAG.id]
                changed = True; changed_fields.append("tag+zbb")

            if changed:
                try:
                    nb_vm.save()
                    loging(f"[{node_name}] KVM VM updated ({', '.join(changed_fields)}): {vm_name}", "sync")
                    print(f"    ~ {vm_name}  [{', '.join(changed_fields)}]")
                except Exception as e:
                    loging(f"[{node_name}] KVM VM update error {vm_name}: {e}", "error")
            else:
                print(f"    = {vm_name}  → ok (no changes)")
                loging(f"[{node_name}] KVM VM skip (no changes): {vm_name}", "debug")

        sync_kvm_vm_disks(nb_vm, kvm_disks)
        sync_kvm_vm_interfaces(nb_vm, kvm_ifaces, all_macs_cache)

    # Обработка исчезнувших VM
    for nb_vm in netbox_api.virtualization.virtual_machines.filter(cluster_id=nb_cluster.id):
        if nb_vm.name not in kvm_vm_names:
            _handle_missing_vm(nb_vm, missing_vm_behavior)


# --- Точка входа ---

def run(groups=None, missing_vm_behavior=None):
    """
    Запускает синхронизацию KVM VM.
    groups — список групп для фильтрации хостов (опционально).
    missing_vm_behavior — "delete"/"offline", если None — спрашивает.
    """
    kvm_template_id = cfg["kvm_template_id"]
    if not kvm_template_id:
        print("[!] Для синхронизации KVM VM нужна секция [KVM] с template_id в config.ini")
        return

    if missing_vm_behavior is None:
        missing_vm_behavior = select_missing_vm_behavior()

    allowed_hostids = None
    if groups:
        allowed_hostids = {h["hostid"] for g in groups for h in g["hosts"]}

    kvm_hosts = select_kvm_hosts(kvm_template_id, allowed_hostids=allowed_hostids)
    if not kvm_hosts:
        print("[!] KVM-хосты не выбраны, выход.")
        return

    loging("=" * 50, "sync")
    loging(f"Start sync: VM KVM | missing_vm: {missing_vm_behavior}", "sync")

    print(f"\n[KVM] Синхронизация {len(kvm_hosts)} гипервизоров (каждый = отдельный кластер)")
    all_macs_cache = {str(m.mac_address) for m in netbox_api.dcim.mac_addresses.all()}

    for host_info in kvm_hosts:
        sync_kvm_host(host_info, cfg.get("kvm_role_vm"), all_macs_cache, missing_vm_behavior)

    loging("Done: VM KVM", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация KVM VM завершена.")


if __name__ == "__main__":
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        raise SystemExit(1)
    run()
