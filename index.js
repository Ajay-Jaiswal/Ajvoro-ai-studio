// Input:
let arr = [[1,3], [2,6], [8,10], [15,18]]
 
// Output:
// [[1,6], [8,10], [15,18]]
let flaternArray = function(arr){
    let result = []
    for(let i=0;i<arr.length;i++){
        if(Array.isArray(arr[i])){
           result =  result.concat(flaternArray(arr[i]))
        }
        else{
            result.push(arr[i])
        }
    }
    return result;
}

// let checkOverLap = function(arr){
//     let lastNumber 
//     let 
// }

console.log(flaternArray(arr))
