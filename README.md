# PIRT Camera Control
Control/GUI code for the PIRT 1280SciCam camera. 
Developed originally by Kishalay De for the MIRAGE camera at MDM Observatory and adapted by Nate Lourie for the WINTER-Deep camera at Palomar.

Original GUI is in the mirage_gui directory. Extended GUI with remote client is in the src/camera_control repo.

## Installation + Environment Setup
Instructions here for using conda + pip, but can get your python with venv or however you want.

Try this:

1. conda update -n base -c defaults conda
2. go to top level `pirt-camera-control` directory
3. Make a new conda environment in this directory: `conda create --prefix .conda python=3.12`
4. Activate the new environment in this directory: `conda activate ./.conda`
5. Update pip: `pip install --upgrade pip`
6. Install dependencies: `pip install -e .` Alternately if you want to install the dev dependencies: `pip install -e ".[dev]"`

## Workflow for updating tags
Check the current version:
```bash:
python
```
```python:
import pirtcam
print(pirtcam.__version__)
```
Which should print something like: `'1.3.0.dev1+gdbc561fb3'`

Upgrade to tag to the next appropriate version. Here going to 1.3.0 or 1.2.2 is appropriate, the text is saying that we are not yet at v1.3.0. In this case we were previously at v1.2.1.

```bash:
git tag -a v1.3.0 -m "message about the new version"
git push origin v0.2.0
```

## License

This project is licensed under the MIT License.

**Note:** This project depends on [PyQt5](https://www.riverbankcomputing.com/software/pyqt/intro), which is licensed under the GNU General Public License (GPL v3). Users are responsible for complying with that license when installing and using PyQt5.