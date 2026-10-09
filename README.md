# konohana-rmk

RMKの学習用。2行3列のミニキーボードのファームウェア

## メモ

Linux 環境では udev ルールの設定が必要。 `/etc/udev/rules.d/99-rmkdev.rules` に以下の設定を書くこと。
`idVendor` , `idProduct` は、 `keyboard.toml` で設定した値と同じにすること。

```
SUBSYSTEM=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="4c4b", ATTRS{idProduct}=="4643", MODE="0666"
KERNEL=="ttyACM[0-9]*", ATTRS{idVendor}=="2886", MODE="0666"

SUBSYSTEM=="usb", ATTRS{idVendor}=="4c4b", ATTRS{idProduct}=="4643", MODE="0666"
```
