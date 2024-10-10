import axios from 'axios'

type feedback = {
    type: string,
    severity: string,
    details: string,
    id: string
}

export async function submitFeedback(feedback: feedback){
    const resp = await axios.post("/feedback", feedback);
    if (resp.status !== 200){
        throw new Error(`Failed Http: ${resp.status}`)
    }
    return;
}
