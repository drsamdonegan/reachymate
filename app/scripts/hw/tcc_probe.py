"""Read permission state from the exact process that will own media."""

from app.scripts.hw.common import parser, versions, write_report
from app.services.system.tcc import camera_devices, permission_status


def main():
    args = parser("tcc_probe").parse_args()
    write_report(
        args.output,
        {
            "status": "measured",
            "tcc": permission_status(),
            "cameras": camera_devices(),
            "versions": versions(),
        },
    )


if __name__ == "__main__":
    main()
