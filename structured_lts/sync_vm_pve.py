#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# sync_vm_pve.py — синхронизация VM Proxmox VE (QEMU + LXC) → NetBox
# Запускается напрямую или через main.py

import fnmatch

from proxmoxer import ProxmoxAPI

from common import (
    cfg, zabbix_api, netbox_api,
    ZABBIX_TAG,
    loging, compact_text,
    get_or_create_tag, get_or_create_cluster_type,
    nb_find_device,
    select_groups, select_missing_vm_behavior,
    _handle_missing_vm, init_resources,
)


# --- Вспомогательные функции парсинга ---

def parse_mac_from_iface(iface_str):
    for part in iface_str.split(","):
        key, _, val = part.partition("=")
        if key.strip() in ("virtio", "e1000e", "e1000", "rtl8139", "vmxnet3"):
            return val.strip()
    return None


def vm_pve_status_to_nb(pve_status, is_template):
    if is_template:
        return "staged"
    return {"running": "active", "stopped": "offline", "paused": "planned"}.get(
        pve_status, "failed"
    )


def parse_disk_size_mb(size_str):
    s = size_str.strip()
    if s.endswith("T"): return int(s[:-1]) * 1_000_000
    if s.endswith("G"): return int(s[:-1]) * 1_000
    if s.endswith("M"): return int(s[:-1])
    return int(s)


def parse_lxc_disks(config_lxc, node_name):
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
    interfaces = []
    for key, val in config_vm.items():
        if key.startswith("net"):
            interfaces.append({
                "name":    key,
                "mac":     parse_mac_from_iface(val),
                "enabled": "link_down" not in val,
            })
    return interfaces


# --- NetBox: кластер per-нода ---

def get_or_create_pve_cluster_for_node(node_name):
    """Получает/создаёт кластер NetBox с именем ноды, привязывает device."""
    nb_cluster = netbox_api.virtualization.clusters.get(name=node_name)
    if not nb_cluster:
        cluster_type = get_or_create_cluster_type("Proxmox VE")
        try:
            nb_cluster = netbox_api.virtualization.clusters.create(
                name=node_name, type=cluster_type.id, status="active"
            )
            loging(f"[PVE] Cluster created: {node_name}", "sync")
            print(f"  [+] Кластер NetBox создан: {node_name} (тип Proxmox VE)")
        except Exception:
            nb_cluster = netbox_api.virtualization.clusters.get(name=node_name)

    if not nb_cluster:
        loging(f"[PVE] Failed to get/create cluster for {node_name}", "error")
        return None

    device = nb_find_device(node_name)
    if device:
        if not device.cluster or device.cluster.id != nb_cluster.id:
            try:
                device.cluster = {"id": nb_cluster.id}
                device.save()
                loging(f"[PVE] Device {node_name} → cluster {node_name}", "sync")
                print(f"  [~] Device {node_name} привязан к кластеру {node_name}")
            except Exception as e:
                loging(f"[PVE] Device cluster bind error {node_name}: {e}", "error")
                print(f"  [!] Ошибка привязки device к кластеру: {e}")
    else:
        loging(f"[PVE] Device not found in NetBox: {node_name}", "error")
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


# --- Синхронизация дисков и интерфейсов VM ---

def sync_vm_disks_nb(nb_vm, pve_disks):
    """Синхронизирует virtual_disks VM. Пустой список → пропуск (защита данных)."""
    if not pve_disks:
        nb_count = len(list(netbox_api.virtualization.virtual_disks.filter(virtual_machine_id=nb_vm.id)))
        loging(f"[{nb_vm.name}] PVE disks: empty — skip sync (protect {nb_count} existing)", "debug")
        print(f"      Дисков в PVE: 0  в NetBox: {nb_count}  → пропуск (нет данных)")
        return

    pve_paths   = {d["path"] for d in pve_disks}
    nb_vm_disks = {
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
    """Синхронизирует интерфейсы VM. Пустой список → пропуск (защита данных)."""
    if not pve_ifaces:
        loging(f"[{nb_vm.name}] PVE ifaces: empty — skip sync", "debug")
        return

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


# --- Выбор PVE-хостов ---

def get_pve_hosts_from_zabbix(template_id, allowed_hostids=None):
    result = []
    hosts = zabbix_api.host.get(templateids=template_id, selectMacros=["macro", "value"])

    if allowed_hostids is not None:
        allowed_set = {str(h) for h in allowed_hostids}
        hosts = [h for h in hosts if str(h["hostid"]) in allowed_set]

    templates = zabbix_api.template.get(templateids=template_id, selectMacros=["macro", "value"])
    template_macros = {}
    for tmpl in templates:
        for m in tmpl["macros"]:
            template_macros[m["macro"]] = m["value"]

    for host in hosts:
        data = dict(template_macros)
        for m in host["macros"]:
            data[m["macro"]] = m["value"]

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
    print("\nЗагружаю PVE-кластеры из Zabbix...")
    hosts = get_pve_hosts_from_zabbix(template_id, allowed_hostids=allowed_hostids)

    if not hosts:
        if allowed_hostids is not None:
            print("[!] PVE-хосты с шаблоном не найдены в выбранных группах.")
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
        filtered = [h for h in hosts if any(fnmatch.fnmatch(h["zabbix_name"], p) for p in active_patterns)]

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


# --- Основная синхронизация ---

def sync_pve_cluster(cluster_info, allowed_nodes=None, missing_vm_behavior="delete"):
    """Синхронизирует ноды PVE-кластера или standalone-ноды с NetBox."""
    from common import ZABBIX_TAG  # актуальный глобал

    role_vm_id = cfg.get("pve_role_vm")
    loging(f"[PVE] Start: {cluster_info['zabbix_name']}", "sync")
    print(f"\n[PVE] Подключение: {cluster_info['zabbix_name']} ({cluster_info['host']})")
    if allowed_nodes:
        print(f"  Фильтр нод: {', '.join(sorted(allowed_nodes))}")

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

    try:
        nodes = proxmox.nodes.get()
    except Exception as e:
        loging(f"[PVE] nodes.get() failed {cluster_info['zabbix_name']}: {e}", "error")
        print(f"  [!] Не удалось получить список нод: {e}")
        return

    # Реальный PVE-кластер → обходим все ноды (allowed_nodes игнорируется).
    # Standalone → фильтруем по allowed_nodes.
    try:
        cluster_status = proxmox.cluster.status.get()
        is_real_cluster = any(e["type"] == "cluster" for e in cluster_status)
    except Exception:
        is_real_cluster = False

    effective_allowed_nodes = None if is_real_cluster else allowed_nodes

    if is_real_cluster:
        print(f"  Режим: PVE-кластер — обходим все ноды")
    else:
        print(f"  Режим: standalone-нода")

    all_macs_cache = {str(m.mac_address) for m in netbox_api.dcim.mac_addresses.all()}

    for node in nodes:
        node_name = node["node"]

        if effective_allowed_nodes and node_name not in effective_allowed_nodes:
            loging(f"[PVE] Node not in selection, skip: {node_name}", "sync")
            print(f"  [~] Нода пропущена (не выбрана): {node_name}")
            continue

        if node["status"] != "online":
            loging(f"[PVE] Node offline, skip: {node_name}", "sync")
            print(f"  [~] Нода offline, пропускаем: {node_name}")
            continue

        print(f"\n  [>] Нода: {node_name}")

        nb_cluster = get_or_create_pve_cluster_for_node(node_name)
        if not nb_cluster:
            print(f"  [!] Не удалось создать/получить кластер для {node_name}, пропускаем")
            continue

        host_dev     = nb_find_device(node_name)
        pve_vm_names = set()

        # ── QEMU VM ──────────────────────────────────────────────────────────
        try:
            all_vms_on_node = proxmox.nodes(node_name).qemu.get()
        except Exception as e:
            loging(f"[PVE] qemu.get() failed on {node_name}: {e}", "error")
            print(f"  [!] Нода {node_name}: не удалось получить список VM: {e}")
            all_vms_on_node = []

        print(f"    QEMU VM: {len(all_vms_on_node)}")

        for vm in all_vms_on_node:
            try:
                config_vm = proxmox.nodes(node_name).qemu(vm["vmid"]).config.get()
            except Exception as e:
                loging(f"[PVE] config.get error vmid={vm['vmid']}: {e}", "error")
                continue

            is_template = bool(vm.get("template", 0))
            vm_nb_name  = config_vm["name"]
            vm_serial   = str(vm["vmid"])
            pve_vm_names.add(vm_nb_name)

            pve_disks  = parse_vm_disks(config_vm, node_name)
            pve_ifaces = parse_vm_interfaces(config_vm)
            nb_status  = vm_pve_status_to_nb(vm["status"], is_template)

            nb_vm = netbox_api.virtualization.virtual_machines.get(
                name=vm_nb_name, cluster_id=nb_cluster.id
            )

            if nb_vm is None:
                create_data = {
                    "name":    vm_nb_name,
                    "cluster": nb_cluster.id,
                    "status":  nb_status,
                    "vcpus":   config_vm.get("cores", 1),
                    "memory":  config_vm.get("memory", 0),
                    "serial":  vm_serial,
                }
                if role_vm_id: create_data["role"]   = role_vm_id
                if host_dev:   create_data["device"] = host_dev.id
                if "description" in config_vm:
                    create_data["comments"] = compact_text(config_vm["description"])

                tags = []
                if ZABBIX_TAG: tags.append(ZABBIX_TAG.id)
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
                changed = False
                changed_fields = []

                if nb_vm.status and nb_vm.status.value != nb_status:
                    nb_vm.status = nb_status; changed = True; changed_fields.append(f"status→{nb_status}")
                else:
                    loging(f"[{node_name}] VM skip status (ok): {vm_nb_name}", "debug")

                if nb_vm.vcpus != config_vm.get("cores", 1):
                    nb_vm.vcpus = config_vm.get("cores", 1); changed = True; changed_fields.append("vcpus")
                else:
                    loging(f"[{node_name}] VM skip vcpus (ok): {vm_nb_name}", "debug")

                if nb_vm.memory != int(config_vm.get("memory", 0)):
                    nb_vm.memory = config_vm.get("memory", 0); changed = True; changed_fields.append("memory")
                else:
                    loging(f"[{node_name}] VM skip memory (ok): {vm_nb_name}", "debug")

                if (nb_vm.serial or "") != vm_serial:
                    nb_vm.serial = vm_serial; changed = True; changed_fields.append("serial")
                else:
                    loging(f"[{node_name}] VM skip serial (ok): {vm_nb_name}", "debug")

                if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                    nb_vm.role = role_vm_id; changed = True; changed_fields.append("role")

                if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                    nb_vm.device = host_dev.id; changed = True; changed_fields.append("device")

                if not nb_vm.cluster or nb_vm.cluster.id != nb_cluster.id:
                    nb_vm.cluster = nb_cluster.id; changed = True; changed_fields.append("cluster")

                descr = compact_text(config_vm.get("description", ""))
                if descr:
                    cur_comments = (nb_vm.comments or "").strip()
                    if cur_comments != descr:
                        nb_vm.comments = descr; changed = True; changed_fields.append("comments")
                    else:
                        loging(f"[{node_name}] VM skip comments (ok): {vm_nb_name}", "debug")

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
            print(f"  [!] Нода {node_name}: не удалось получить список LXC: {e}")
            all_cts_on_node = []

        print(f"    LXC: {len(all_cts_on_node)}")

        for ct in all_cts_on_node:
            try:
                config_ct = proxmox.nodes(node_name).lxc(ct["vmid"]).config.get()
            except Exception as e:
                loging(f"[PVE] lxc config.get error vmid={ct['vmid']}: {e}", "error")
                continue

            is_template = bool(ct.get("template", 0))
            ct_nb_name  = ct["name"]
            ct_serial   = str(ct["vmid"])
            pve_vm_names.add(ct_nb_name)

            pve_disks  = parse_lxc_disks(config_ct, node_name)
            pve_ifaces = parse_lxc_interfaces(config_ct)
            nb_status  = vm_pve_status_to_nb(ct["status"], is_template)

            nb_vm = netbox_api.virtualization.virtual_machines.get(
                name=ct_nb_name, cluster_id=nb_cluster.id
            )

            if nb_vm is None:
                create_data = {
                    "name":    ct_nb_name,
                    "cluster": nb_cluster.id,
                    "status":  nb_status,
                    "vcpus":   config_ct.get("cores", 1),
                    "memory":  config_ct.get("memory", 0),
                    "serial":  ct_serial,
                }
                if role_vm_id: create_data["role"]   = role_vm_id
                if host_dev:   create_data["device"] = host_dev.id
                if "description" in config_ct:
                    create_data["comments"] = compact_text(config_ct["description"])

                tags = []
                if ZABBIX_TAG: tags.append(ZABBIX_TAG.id)
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
                    nb_vm.status = nb_status; changed = True; changed_fields.append(f"status→{nb_status}")
                else:
                    loging(f"[{node_name}] LXC skip status (ok): {ct_nb_name}", "debug")

                if nb_vm.vcpus != config_ct.get("cores", 1):
                    nb_vm.vcpus = config_ct.get("cores", 1); changed = True; changed_fields.append("vcpus")
                else:
                    loging(f"[{node_name}] LXC skip vcpus (ok): {ct_nb_name}", "debug")

                if nb_vm.memory != int(config_ct.get("memory", 0)):
                    nb_vm.memory = config_ct.get("memory", 0); changed = True; changed_fields.append("memory")
                else:
                    loging(f"[{node_name}] LXC skip memory (ok): {ct_nb_name}", "debug")

                if (nb_vm.serial or "") != ct_serial:
                    nb_vm.serial = ct_serial; changed = True; changed_fields.append("serial")
                else:
                    loging(f"[{node_name}] LXC skip serial (ok): {ct_nb_name}", "debug")

                if role_vm_id and (not nb_vm.role or nb_vm.role.id != role_vm_id):
                    nb_vm.role = role_vm_id; changed = True; changed_fields.append("role")

                if host_dev and (not nb_vm.device or nb_vm.device.id != host_dev.id):
                    nb_vm.device = host_dev.id; changed = True; changed_fields.append("device")

                if not nb_vm.cluster or nb_vm.cluster.id != nb_cluster.id:
                    nb_vm.cluster = nb_cluster.id; changed = True; changed_fields.append("cluster")

                descr = compact_text(config_ct.get("description", ""))
                if descr:
                    cur_comments = (nb_vm.comments or "").strip()
                    if cur_comments != descr:
                        nb_vm.comments = descr; changed = True; changed_fields.append("comments")
                    else:
                        loging(f"[{node_name}] LXC skip comments (ok): {ct_nb_name}", "debug")

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

        # Обработка исчезнувших VM на этой ноде
        for nb_vm in netbox_api.virtualization.virtual_machines.filter(cluster_id=nb_cluster.id):
            if nb_vm.name not in pve_vm_names:
                _handle_missing_vm(nb_vm, missing_vm_behavior)

    loging(f"[PVE] Done: {cluster_info['zabbix_name']}", "sync")


# --- Точка входа ---

def run(groups=None, missing_vm_behavior=None):
    """
    Запускает синхронизацию PVE VM.
    groups — список групп для фильтрации хостов (опционально).
    missing_vm_behavior — "delete"/"offline", если None — спрашивает.
    """
    pve_template_id = cfg["pve_template_id"]
    if not pve_template_id:
        print("[!] Для синхронизации PVE VM нужна секция [PROXMOX] с template_id в config.ini")
        return

    if missing_vm_behavior is None:
        missing_vm_behavior = select_missing_vm_behavior()

    allowed_hostids = None
    if groups:
        allowed_hostids = {h["hostid"] for g in groups for h in g["hosts"]}

    pve_clusters = select_pve_clusters(pve_template_id, allowed_hostids=allowed_hostids)
    if not pve_clusters:
        print("[!] Кластеры PVE не выбраны, выход.")
        return

    loging("=" * 50, "sync")
    loging(f"Start sync: VM PVE | missing_vm: {missing_vm_behavior}", "sync")

    processed_clusters = {}
    for h in pve_clusters:
        key = f"{h['host']}:{h['port']}"
        if key not in processed_clusters:
            processed_clusters[key] = {"entry": h, "nodes": set()}
        processed_clusters[key]["nodes"].add(h["zabbix_name"].split(".")[0])

    for key, cluster_data in processed_clusters.items():
        sync_pve_cluster(
            cluster_data["entry"],
            allowed_nodes=cluster_data["nodes"],
            missing_vm_behavior=missing_vm_behavior,
        )

    loging("Done: VM PVE", "sync")
    loging("=" * 50, "sync")
    print("\n[✓] Синхронизация PVE VM завершена.")


if __name__ == "__main__":
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        raise SystemExit(1)
    run()
