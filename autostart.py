"""Register Attendance Automation to run at sign-in, unlock, and system resume."""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AttendanceAutomation"
TASK_NAME = "AttendanceAutomation"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _startup_parts(executable=None, script=None):
    current_app = executable is None
    if executable is None:
        executable = sys.executable
    if script is None and current_app and not getattr(sys, "frozen", False):
        script = os.path.abspath(sys.argv[0])
    return executable, script


def startup_command(executable=None, script=None):
    """Build a quoted command for either the packaged app or the Python entry point."""
    executable, script = _startup_parts(executable, script)
    args = [executable]
    if script:
        args.append(script)
    return subprocess.list2cmdline(args)


def scheduled_task_xml(executable, user, script=None):
    """Build a per-user task that relaunches the app at sign-in, unlock, and resume."""
    ET.register_namespace("", TASK_NAMESPACE)

    def tag(name):
        return f"{{{TASK_NAMESPACE}}}{name}"

    root = ET.Element(tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(root, tag("RegistrationInfo"))
    ET.SubElement(registration, tag("Author")).text = "Attendance Automation"
    ET.SubElement(registration, tag("Description")).text = (
        "Starts Attendance Automation when this user signs in, unlocks Windows, or resumes the laptop."
    )
    triggers = ET.SubElement(root, tag("Triggers"))
    logon = ET.SubElement(triggers, tag("LogonTrigger"))
    ET.SubElement(logon, tag("Enabled")).text = "true"
    ET.SubElement(logon, tag("UserId")).text = user
    ET.SubElement(logon, tag("Delay")).text = "PT10S"
    unlock = ET.SubElement(triggers, tag("SessionStateChangeTrigger"))
    ET.SubElement(unlock, tag("Enabled")).text = "true"
    ET.SubElement(unlock, tag("UserId")).text = user
    ET.SubElement(unlock, tag("StateChange")).text = "SessionUnlock"
    ET.SubElement(unlock, tag("Delay")).text = "PT10S"
    resume = ET.SubElement(triggers, tag("EventTrigger"))
    ET.SubElement(resume, tag("Enabled")).text = "true"
    ET.SubElement(resume, tag("Subscription")).text = (
        "<QueryList><Query Id='0' Path='System'><Select Path='System'>"
        "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]"
        "</Select></Query></QueryList>"
    )
    ET.SubElement(resume, tag("Delay")).text = "PT10S"

    principal = ET.SubElement(ET.SubElement(root, tag("Principals")), tag("Principal"), {"id": "CurrentUser"})
    ET.SubElement(principal, tag("UserId")).text = user
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(root, tag("Settings"))
    ET.SubElement(settings, tag("MultipleInstancesPolicy")).text = "IgnoreNew"
    ET.SubElement(settings, tag("StartWhenAvailable")).text = "true"
    ET.SubElement(settings, tag("DisallowStartIfOnBatteries")).text = "false"
    ET.SubElement(settings, tag("StopIfGoingOnBatteries")).text = "false"
    ET.SubElement(settings, tag("ExecutionTimeLimit")).text = "PT0S"

    actions = ET.SubElement(root, tag("Actions"), {"Context": "CurrentUser"})
    action = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(action, tag("Command")).text = executable
    if script:
        ET.SubElement(action, tag("Arguments")).text = subprocess.list2cmdline([script])
    ET.SubElement(action, tag("WorkingDirectory")).text = os.path.dirname(script or executable)
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def _delete_startup_task():
    query = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True, timeout=15,
    )
    if query.returncode == 0:
        result = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode:
            raise OSError((result.stderr or result.stdout).strip() or "Could not remove the scheduled startup task.")


def set_start_on_login(enabled, executable=None, script=None):
    """Add or remove per-user sign-in, unlock, and resume startup triggers."""
    if os.name != "nt":
        raise OSError("Automatic sign-in startup is only available on Windows.")

    import winreg

    if enabled:
        executable, script = _startup_parts(executable, script)
        user = subprocess.run(
            ["whoami.exe"], capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
        xml = scheduled_task_xml(executable, user, script)
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".xml", delete=False,
            ) as task_file:
                task_file.write(xml)
                path = task_file.name
            result = subprocess.run(
                ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode:
                raise OSError((result.stderr or result.stdout).strip() or "Could not register the scheduled startup task.")
        finally:
            if path and os.path.exists(path):
                os.unlink(path)
    else:
        _delete_startup_task()

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass
