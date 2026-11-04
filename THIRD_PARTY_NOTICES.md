# Third-party notices

The MIT licence in `LICENSE` covers Winds Aloft's own code. These other parts have their own terms.

## Included in WindsAloft.exe

- **Python runtime:** the executable contains the Python interpreter and standard library, under the [Python Software Foundation License](https://docs.python.org/3/license.html).
- **PyInstaller bootloader:** the executable is built with [PyInstaller](https://pyinstaller.org/), whose licence allows distributing programs built with it.

## Used by the tool, but not included

- **Open-Meteo:** weather data comes from [Open-Meteo.com](https://open-meteo.com/) under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Winds are interpolated to 1,000 ft levels and adjusted for the Sim est. column. Its free service is for non-commercial use, subject to its [terms](https://open-meteo.com/en/terms).
- **Forecast models:** NOAA GFS and ECMWF IFS, provided through Open-Meteo.
- **SimBrief:** flight plans are fetched from your own SimBrief account through its public API.
- **SimConnect.dll:** this Microsoft file is part of the MSFS SDK. It isn't included, and it's subject to the SDK's licence. The tool loads a copy already on your PC.
- **Microsoft Edge or Google Chrome:** the tool's window uses the browser already installed on your PC.

## Trademarks

Winds Aloft is an independent project. It isn't affiliated with or endorsed by Microsoft, Asobo Studio, Navigraph, SimBrief, Open-Meteo, NOAA or ECMWF. Their names are used only to describe what the tool works with.
