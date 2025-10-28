# Dify File Upload Application

A simple and elegant web application for uploading files to update code. Built with Flask and featuring a modern, responsive user interface.

## Features

- 📁 **File Upload**: Upload code files through a beautiful web interface
- 📂 **File Management**: View and delete uploaded files
- 🔒 **Security**: File type validation and secure filename handling
- 🎨 **Modern UI**: Clean, responsive design with gradient themes
- 📝 **Supported File Types**: txt, py, js, html, css, json, md, yml, yaml, xml, sh

## Installation

1. Clone the repository:
```bash
git clone https://github.com/drhouta92/dify.git
cd dify
```

2. Create a virtual environment (recommended):
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. Start the application:
```bash
python app.py
```

2. Open your browser and navigate to:
```
http://localhost:5000
```

3. Upload files using the web interface:
   - Click "Choose File" to select a file
   - Click "Upload File" to upload
   - View uploaded files in the list below
   - Click "View" to download/view a file
   - Click "Delete" to remove a file

## Configuration

The application can be configured by modifying the following variables in `app.py`:

- `UPLOAD_FOLDER`: Directory where uploaded files are stored (default: 'uploads')
- `ALLOWED_EXTENSIONS`: Set of allowed file extensions
- `MAX_FILE_SIZE`: Maximum file size in bytes (default: 16MB)

## Security Features

- Secure filename handling using `werkzeug.utils.secure_filename`
- File type validation based on extension
- File size limits
- Upload folder isolation

## Project Structure

```
dify/
├── app.py              # Main Flask application
├── templates/
│   └── index.html      # Web interface template
├── uploads/            # Directory for uploaded files (created automatically)
├── requirements.txt    # Python dependencies
├── .gitignore         # Git ignore file
└── README.md          # This file
```

## Development

To run in development mode with debug enabled:

```bash
python app.py
```

The application will be accessible at `http://localhost:5000` with auto-reload enabled.

## License

This project is open source and available under the MIT License.