#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# main.py — интерактивный запуск синхронизации Zabbix → NetBox
# Запускает один из пяти скриптов синхронизации по выбору пользователя.

from common import init_resources, select_groups, select_missing_vm_behavior, loging

import sync_inventory
import sync_hardware
import sync_vm_pve
import sync_vm_kvm
import sync_network


def select_mode():
    """Меню выбора режима синхронизации."""
    print("\n" + "=" * 50)
    print("  Что синхронизируем?")
    print("=" * 50)
    print("  1. Устройства    (serial, platform, tags, comments)")
    print("  2. Диски         (inventory items: serial, model, status)")
    print("  3. VM Proxmox    (QEMU + LXC → NetBox)")
    print("  4. VM KVM        (KVM → NetBox через Zabbix items)")
    print("  5. Сетевые       (serial, interfaces, descriptions)")
    print("=" * 50)

    while True:
        choice = input("Выберите режим [1/2/3/4/5]: ").strip()
        if choice in ("1", "2", "3", "4", "5"):
            return choice
        print("  [!] Введите 1, 2, 3, 4 или 5")


def _print_summary(mode, groups=None, extra=None):
    """Выводит сводку перед запуском."""
    print("\n" + "=" * 50)
    print(f"  Режим:  {mode}")
    if groups:
        total_hosts = sum(len(g["hosts"]) for g in groups)
        print(f"  Групп:  {len(groups)}  ({total_hosts} хостов)")
    if extra:
        for k, v in extra.items():
            print(f"  {k}:  {v}")
    print("=" * 50)


def _confirm():
    """Запрос подтверждения."""
    choice = input("  Запускаем? [y/n]: ").strip().lower()
    if choice != "y":
        print("  → Отменено.")
        return False
    return True


def main():
    # --- Шаг 1: проверка подключения к NetBox ---
    print("\n[*] Проверяю подключение к NetBox (тег + роль)...")
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        return
    print("[✓] NetBox: OK")

    # --- Шаг 2: выбор режима ---
    mode = select_mode()

    # --- Режим 1: устройства ---
    if mode == "1":
        print("\n  [Шаг: выбор групп Zabbix]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

        _print_summary(mode="устройства", groups=groups)
        if not _confirm():
            return

        loging(f"Start: mode=inventory | groups={len(groups)}", "sync")
        sync_inventory.run(groups=groups)

    # --- Режим 2: диски ---
    elif mode == "2":
        print("\n  [Шаг: выбор групп Zabbix]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

        _print_summary(mode="диски", groups=groups)
        if not _confirm():
            return

        loging(f"Start: mode=hardware | groups={len(groups)}", "sync")
        sync_hardware.run(groups=groups)

    # --- Режим 3: VM Proxmox ---
    elif mode == "3":
        missing = select_missing_vm_behavior()

        print("\n  [Шаг: выбор групп / PVE-хостов]")
        groups = select_groups()

        _print_summary(mode="VM Proxmox", groups=groups,
                       extra={"Исчезнувшие VM": missing})
        if not _confirm():
            return

        loging(f"Start: mode=pve | missing_vm={missing}", "sync")
        sync_vm_pve.run(groups=groups, missing_vm_behavior=missing)

    # --- Режим 4: VM KVM ---
    elif mode == "4":
        missing = select_missing_vm_behavior()

        print("\n  [Шаг: выбор групп / KVM-хостов]")
        groups = select_groups()

        _print_summary(mode="VM KVM", groups=groups,
                       extra={"Исчезнувшие VM": missing})
        if not _confirm():
            return

        loging(f"Start: mode=kvm | missing_vm={missing}", "sync")
        sync_vm_kvm.run(groups=groups, missing_vm_behavior=missing)

    # --- Режим 5: сетевые устройства ---
    elif mode == "5":
        print("\n  [Шаг: выбор групп Zabbix (Net/*)]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

        _print_summary(mode="сетевые устройства", groups=groups)
        if not _confirm():
            return

        loging(f"Start: mode=network | groups={len(groups)}", "sync")
        sync_network.run(groups=groups)


if __name__ == "__main__":
    main()
