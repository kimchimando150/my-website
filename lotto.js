const min= 1;
const max = 46;
let num1 = Math.floor(Math.random() * (max - min)) + min;
let num2 = Math.floor(Math.random() * (max - min)) + min;
let num3 = Math.floor(Math.random() * (max - min)) + min;
let num4 = Math.floor(Math.random() * (max - min)) + min;
let num5 = Math.floor(Math.random() * (max - min)) + min;
let num6 = Math.floor(Math.random() * (max - min)) + min;
let list = [num1, num2, num3, num4, num5, num6];
list.sort((a, b) => a - b);
let username;
let randomNum = Math.floor(Math.random()* (max - min)) + min;

document.getElementById("mySubmit").onclick = function(){
    username = document.getElementById("myText").value;
    if (username != "") {
        document.getElementById("myH1").textContent = `안녕하세요, ${username}. 당신의 행운의 번호를 뽑아드리겠습니다.`;
        document.getElementById("mySubmit").remove();
        let newButton = document.createElement("button");
        newButton.textContent = "뽑기";
        document.body.appendChild(newButton);

        newButton.onclick = function(){
            document.getElementById("myP").textContent = list
        }
    }
    else{
        document.getElementById("myH1").textContent = `사용자 닉네임을 입력해 주세요`
    }
}
