"""
Tests for the file upload application.
"""

import os
import tempfile
import pytest
from app import app


@pytest.fixture
def client():
    """Create a test client for the application."""
    app.config['TESTING'] = True
    app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()
    
    with app.test_client() as client:
        yield client
    
    # Cleanup
    import shutil
    if os.path.exists(app.config['UPLOAD_FOLDER']):
        shutil.rmtree(app.config['UPLOAD_FOLDER'])


def test_index_page(client):
    """Test that the index page loads successfully."""
    response = client.get('/')
    assert response.status_code == 200
    assert b'Dify File Upload' in response.data


def test_upload_valid_file(client):
    """Test uploading a valid file."""
    data = {
        'file': (tempfile.NamedTemporaryFile(suffix='.txt', delete=False), 'test.txt')
    }
    response = client.post('/upload', data=data, follow_redirects=True)
    assert response.status_code == 200
    assert b'uploaded successfully' in response.data


def test_upload_no_file(client):
    """Test uploading with no file selected."""
    response = client.post('/upload', data={}, follow_redirects=True)
    assert response.status_code == 200
    assert b'No file' in response.data


def test_upload_invalid_extension(client):
    """Test uploading a file with invalid extension."""
    data = {
        'file': (tempfile.NamedTemporaryFile(suffix='.exe', delete=False), 'test.exe')
    }
    response = client.post('/upload', data=data, follow_redirects=True)
    assert response.status_code == 200
    assert b'not allowed' in response.data


def test_allowed_file():
    """Test the allowed_file function."""
    from app import allowed_file
    
    assert allowed_file('test.py') == True
    assert allowed_file('test.txt') == True
    assert allowed_file('test.js') == True
    assert allowed_file('test.exe') == False
    assert allowed_file('test') == False
