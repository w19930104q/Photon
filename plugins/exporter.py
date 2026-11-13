"""Support for exporting the results."""
import csv
import io
import json
import os

from core.utils import atomic_write


def exporter(directory, method, datasets):
    """Export the results atomically.

    Each format is rendered fully in memory and published through a single
    os.replace via atomic_write, so an interrupted export cannot leave a
    truncated exported.json/exported.csv. The two formats use distinct file
    names and the per-dataset .txt files are never touched here.
    """
    method = method.lower()
    if method == 'json':
        json_string = json.dumps(datasets, indent=4)
        atomic_write(os.path.join(directory, 'exported.json'),
                     json_string)
    elif method == 'csv':
        buffer = io.StringIO(newline='')
        csv_writer = csv.writer(
            buffer, delimiter=',', quoting=csv.QUOTE_MINIMAL)
        for key, values in datasets.items():
            if values is None:
                csv_writer.writerow([key])
            else:
                csv_writer.writerow([key] + [str(value) for value in values])
        atomic_write(os.path.join(directory, 'exported.csv'),
                     buffer.getvalue())
