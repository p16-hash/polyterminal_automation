#!/usr/bin/env python3
"""
Polymarket Trading Bot Launcher

Main menu for all trading tools.
"""

import os
import sys

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_CYAN = "\033[46m"
    WHITE = "\033[97m"
    BLACK = "\033[30m"

MENU_ITEMS = [
    ("1", "Live Trading", "trade"),
    ("2", "Redeem All", "redeemall"),
    ("3", "Redeem Market", "redeem"),
    ("4", "Check Balance", "check_balance"),
    ("5", "Generate Keys", "generate_keys"),
    ("6", "Set Allowances", "set_allowances"),
]

def clear_screen():
    os.system("clear" if os.name != "nt" else "cls")

def show_menu():
    clear_screen()
    print(f"\n{Colors.BOLD}{'='*50}{Colors.RESET}")
    print(f"{Colors.CYAN}{Colors.BOLD}  POLYMARKET TRADING BOT{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*50}{Colors.RESET}\n")
    
    for key, name, _ in MENU_ITEMS:
        print(f"  {Colors.BG_CYAN}{Colors.BLACK} {key} {Colors.RESET}  {name}")
    
    print(f"\n  {Colors.DIM}Q  Quit{Colors.RESET}")
    print(f"\n{Colors.BOLD}{'='*50}{Colors.RESET}")
    print(f"\n{Colors.DIM}Select option:{Colors.RESET} ", end="", flush=True)

def get_single_key():
    """Get single keypress without Enter."""
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except:
        return input().strip().lower()

def run_utility_script(module_name, display_name):
    """Run a utility script and show post-action menu."""
    clear_screen()
    print(f"\n{Colors.CYAN}{Colors.BOLD}=== {display_name} ==={Colors.RESET}\n")
    
    try:
        if module_name == "check_balance":
            from check_balance import check_balance
            check_balance()
        elif module_name == "generate_keys":
            from generate_keys import main as gen_main
            gen_main()
        elif module_name == "set_allowances":
            from set_allowances import main as allow_main
            allow_main()
        elif module_name == "redeem":
            from redeem import main as redeem_main
            redeem_main()
        elif module_name == "redeemall":
            from redeemall import main as redeemall_main
            redeemall_main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
    
    return show_post_action_menu()

def run_trading():
    """Run live trading (handles its own Q key)."""
    clear_screen()
    try:
        from trade import main as trade_main
        result = trade_main()
        if result == "menu":
            return True
        return True
    except KeyboardInterrupt:
        return True
    except SystemExit:
        return True
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        input("\nPress Enter to continue...")
        return True

def show_post_action_menu():
    """Show menu after utility script completes. Returns 'again', 'menu', or 'quit'."""
    print(f"\n{Colors.BOLD}{'─'*40}{Colors.RESET}")
    print(f"  {Colors.BG_GREEN}{Colors.WHITE} R {Colors.RESET}  Run again")
    print(f"  {Colors.BG_YELLOW}{Colors.BLACK} M {Colors.RESET}  Main menu")
    print(f"\n{Colors.DIM}Select:{Colors.RESET} ", end="", flush=True)
    
    key = get_single_key()
    
    if key == 'r':
        return "again"
    else:
        return "menu"

def main():
    while True:
        show_menu()
        key = get_single_key()
        
        if key == 'q':
            clear_screen()
            print(f"\n{Colors.DIM}Goodbye!{Colors.RESET}\n")
            break
        
        for menu_key, display_name, module_name in MENU_ITEMS:
            if key == menu_key:
                if module_name == "trade":
                    run_trading()
                else:
                    while True:
                        result = run_utility_script(module_name, display_name)
                        if result == "again":
                            continue
                        else:
                            break
                break

if __name__ == "__main__":
    main()
