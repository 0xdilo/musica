# Shazamma e salva

Install the signed shortcut from the private Musica Capture page. Sign in with your Navidrome account, copy the connection configuration, then paste it into the shortcut's single import question. The distributed file contains no server address or credentials.

The shortcut recognizes a recording with Shazam, sends its artist and title to the capture endpoint, and shows the server's acknowledgement. The server queues acquisition and import separately. An acknowledgement means the request was saved; it does not mean the download has completed. Progress and failures remain visible on the private page. Ready tracks are added to the **Da Shazam** playlist without replacing its existing entries.

Assign the shortcut to Back Tap or invoke it with Siri. The first run needs the usual iOS permissions. Recognition of audio playing through Amperfy, especially with headphones, and use while the phone is locked must be checked on the actual iPhone. The server cannot verify those behaviors remotely.

## Source and signing

`Shazamma-e-salva.cherri` is the editable source. `Shazamma-e-salva.plist` is the compiled, unsigned workflow for inspection. `../web/capture/Shazamma-e-salva.shortcut` is the signed installation artifact.

Compiled with [Cherri](https://github.com/electrikmilk/cherri). The generic workflow was signed through Cherri's RoutineHub/HubSign fallback on Linux. No personal configuration was submitted to the signing service. Signing a future generic version on macOS is also supported with Apple's `shortcuts sign --mode anyone` command. Never send a configured shortcut containing a personal token to a third-party signing service.

To rebuild with Cherri installed:

```sh
cherri shortcuts/Shazamma-e-salva.cherri --share=anyone --output=web/capture/Shazamma-e-salva.shortcut
```

Signing requires the macOS tool or an available signing service. Inspect the generated workflow and verify installation on iPhone after changing it.
