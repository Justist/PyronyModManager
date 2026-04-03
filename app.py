import sys
from PySide6.QtWidgets import QApplication
from pmm.ui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("PyIrony")
    app.setOrganizationName("pyirony")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()