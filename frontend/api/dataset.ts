import axios from 'axios'

export async function upload(file: File,
    setProgress: (value: number) => void
){
    const formData = new FormData();
    formData.append('file', file);
    return axios.post("http://127.0.0.1:8000/upload", formData, {
      headers: {
        'content-type': 'multipart/form-data',
        'Access-Control-Allow-Origin': '*',
      },
      onUploadProgress: (p) => {
        setProgress(Math.round(100 * p.loaded / p.total!));
      }
    }).then(resp => {
        setProgress(100);
        if (resp.status != 200){
            throw new Error(`Ran into error: http:${resp.status} ${resp.statusText} details:${resp.data}`);
        }

    });
}