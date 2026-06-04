const express = require('express');
const axios = require('axios');
const path = require('path');
const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static('public'));

app.get('/proxy/:platform/:user/:repo/:branch/*', async (req, res) => {
    const { platform, user, repo, branch } = req.params;
    const filePath = req.params[0];
    let url = '';

    if (platform === 'github') {
        url = `https://raw.githubusercontent.com/${user}/${repo}/${branch}/${filePath}`;
    } else if (platform === 'gitverse') {
        url = `https://gitverse.ru/api/v1/repos/${user}/${repo}/raw/${filePath}?ref=${branch}`;
    }

    try {
        const response = await axios.get(url, { responseType: 'stream' });
        res.setHeader('Content-Type', response.headers['content-type'] || 'text/plain');
        res.setHeader('Access-Control-Allow-Origin', '*');
        response.data.pipe(res);
    } catch (e) {
        res.status(404).send('File not found or Provider Error');
    }
});

app.listen(PORT, () => console.log(`T2000 running on port ${PORT}`));